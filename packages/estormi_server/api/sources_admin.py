"""Source on/off toggle.

Owns the "configure a source" endpoint the Settings UI exposes: the source
on/off switch with its macOS permission probe.
"""

from __future__ import annotations

import asyncio

import structlog
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from estormi_server.server.limiter import limiter
from estormi_server.server.sources import is_valid_source_slug

log = structlog.get_logger()

router = APIRouter()


# ── Sources: toggle ───────────────────────────────────────────────────────────


class _ToggleSourceBody(BaseModel):
    enabled: bool = True


@router.post("/api/sources/{name}/toggle")
@limiter.limit("30/minute")
async def toggle_source(name: str, request: Request, body: _ToggleSourceBody):
    """Enable/disable a source.

    Activating a source also triggers its macOS permission prompt *now* —
    attributed to Estormi — and verifies the result, so the user grants
    (or is told to grant) access at the moment of activation instead of
    mid-pipeline-run. ``permission`` is the verified outcome, or ``null``
    for sources that need no macOS permission (or when disabling).
    """
    from estormi_server.storage.tools import get_write_lock, sqlite_conn  # noqa: PLC0415

    if not is_valid_source_slug(name):
        raise HTTPException(400, f"Invalid source slug: {name}")
    enabled = body.enabled
    db = sqlite_conn()
    # Leaf INSERT→commit span — serialise on the shared write lock (the
    # persist_source_permission below takes it independently, after this
    # releases). See ``tools._write_lock``.
    async with get_write_lock():
        await db.execute(
            "INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (f"source_{name}_enabled", str(enabled).lower()),
        )
        await db.commit()

    permission = None
    if enabled:
        from estormi_server.server.permissions import ensure_source_permission  # noqa: PLC0415

        # Folder-rooted sources (documents, code) prompt for Files-and-Folders
        # access by probing their configured root; fetch it so the probe runs
        # against the real folder rather than a guess.
        root = None
        async with db.execute("SELECT value FROM settings WHERE key = ?", (f"{name}_root",)) as cur:
            row = await cur.fetchone()
            if row:
                root = row[0]
        try:
            permission = await asyncio.to_thread(ensure_source_permission, name, root)
        except Exception:
            # best-effort: a failed permission probe must not block the
            # toggle itself — the source is still enabled.
            log.exception("source_permission_probe_failed", source=name)
        else:
            # Persist the verified status so the run-time gate (and the UI)
            # can read it without ever re-probing TCC. See
            # server/permission_preflight.py.
            from estormi_server.server.permission_preflight import (
                persist_source_permission,  # noqa: PLC0415
            )

            await persist_source_permission(db, name, permission)

    return {"source": name, "enabled": enabled, "permission": permission}
