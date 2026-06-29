"""Root storage-location endpoints — read the current library dir, and queue a
relocation of the *whole* data dir to a new path.

The base path can't be a row in the ``settings`` table: that table lives inside
``estormi.db`` which lives inside the data dir, so the path is a pointer file
managed by :mod:`memory_core.datadir`. ``POST /api/storage/relocate`` only
*queues* the move (writes a marker); the copy → verify → swap happens at the
next app start in :func:`memory_core.datadir.bootstrap_relocate`, so the UI tells
the user to reopen. The iCloud vault is deliberately out of scope — it syncs to
the iOS companion and must never be swallowed by the library.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path, PurePosixPath

import structlog
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from estormi_server.server.limiter import limiter

router = APIRouter()
log = structlog.get_logger(__name__)


class RelocateRequest(BaseModel):
    """Body of ``POST /api/storage/relocate`` — the destination library dir."""

    to: str


def _free_gb(path: str) -> float | None:
    """Free GB on *path*'s volume, probing the nearest existing ancestor."""
    probe = path
    while not os.path.exists(probe) and os.path.dirname(probe) != probe:
        probe = os.path.dirname(probe)
    try:
        return round(shutil.disk_usage(probe).free / 2**30, 1)
    except OSError:
        return None


def _within(child: str, parent: str) -> bool:
    """True when *child* is *parent* or nested under it (both absolute)."""
    try:
        return os.path.commonpath([child, parent]) == parent
    except ValueError:  # different drives / un-comparable
        return False


@router.get("/api/storage/location")
@limiter.limit("30/minute")
async def storage_location(request: Request):
    """The current library dir, its free space + size, and any queued move."""
    from estormi_server.services import overview as ov  # noqa: PLC0415
    from memory_core.datadir import (  # noqa: PLC0415
        default_data_dir,
        pending_relocation,
        resolve_data_dir,
    )

    current = resolve_data_dir()
    return {
        "dir": current,
        "default": default_data_dir(),
        "freeGb": _free_gb(current),
        "libraryBytes": await ov.cached_dir_size(Path(current)),
        "pending": pending_relocation(),
    }


@router.post("/api/storage/relocate")
@limiter.limit("4/minute")
async def storage_relocate(request: Request, body: RelocateRequest):
    """Queue a move of the whole library to ``body.to`` on the next launch.

    Validates, then writes only the relocation marker — nothing moves now. The
    running app keeps using the current dir until it is reopened.
    """
    from estormi_server.services import overview as ov  # noqa: PLC0415
    from memory_core.audit import log_security_decision  # noqa: PLC0415
    from memory_core.datadir import resolve_data_dir, write_relocation_marker  # noqa: PLC0415

    expanded = os.path.expanduser((body.to or "").strip())
    if not expanded:
        return JSONResponse({"error": "destination path required"}, status_code=400)

    pp = PurePosixPath(expanded)
    if not pp.is_absolute() or ".." in pp.parts:
        return JSONResponse({"error": "path must be absolute (no '..')"}, status_code=400)
    target = os.path.abspath(expanded)

    current = os.path.abspath(resolve_data_dir())
    if target == current:
        return JSONResponse({"error": "that is already the storage location"}, status_code=400)

    # Reject nesting either way — a copy into a sub/parent of itself would recurse.
    if _within(target, current) or _within(current, target):
        return JSONResponse(
            {"error": "the new location cannot be inside the current one (or vice-versa)"},
            status_code=400,
        )

    # Never overlap the iCloud vault (it syncs to the iOS companion).
    try:
        from estormi_ingestion.shared.delivery.vault_sync import vault_dir  # noqa: PLC0415

        vault = os.path.abspath(str(vault_dir()))
        if target == vault or _within(target, vault) or _within(vault, target):
            return JSONResponse(
                {"error": "the storage location cannot overlap the iCloud vault"},
                status_code=400,
            )
    except Exception:  # noqa: BLE001 — vault unresolvable in this env: skip the guard
        pass

    # Destination volume present + writable.
    probe = target
    while not os.path.exists(probe) and os.path.dirname(probe) != probe:
        probe = os.path.dirname(probe)
    if not (os.path.isdir(probe) and os.access(probe, os.W_OK)):
        return JSONResponse(
            {"error": "the destination volume is missing or not writable"}, status_code=400
        )

    # Enough free space for the whole library (+10% headroom).
    library_bytes = await ov.cached_dir_size(Path(current))
    try:
        free = shutil.disk_usage(probe).free
    except OSError:
        free = 0
    if free < int(library_bytes * 1.1):
        need_gb = round(library_bytes * 1.1 / 2**30, 1)
        free_gb = round(free / 2**30, 1)
        return JSONResponse(
            {"error": f"not enough free space ({free_gb} GB free, need ≥{need_gb} GB)"},
            status_code=400,
        )

    write_relocation_marker(current, target)
    log_security_decision(
        decision="accept",
        path="/api/storage/relocate",
        client_host=request.client.host if request.client else "",
        reason=f"storage_relocate:{target}"[:200],
        method="POST",
    )
    log.info("storage.relocate.queued", **{"from": current, "to": target})
    return {
        "ok": True,
        "willMoveOnRestart": True,
        "from": current,
        "to": target,
        "bytes": library_bytes,
    }
