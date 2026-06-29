"""Permission preflight — group every macOS prompt at app launch.

The single contract: **prompts happen here, in the foreground, attributed to
Estormi** — never mid-pipeline-run. :func:`run_preflight` walks every *enabled*
source that needs a macOS permission and probes it (via
``server.permissions.ensure_source_permission``), plus the removable volumes
the app's own working set spans, and persists each verified status to the
``settings`` table.

Downstream, two readers consume that persisted status without ever touching
TCC again:

* the connector run gate (``connectors/permission_gate.py``) skips a
  stage whose status isn't ``authorized``;
* the Settings UI renders the per-source / per-volume state.

Probing is **sequential** on purpose: macOS queues simultaneous TCC prompts
into an unreadable stack, so one-at-a-time keeps the launch experience legible.
"""

from __future__ import annotations

import asyncio
import json
import os

import structlog

log = structlog.get_logger()

_PERMISSION_KEY = "source_{name}_permission"
_VOLUME_KEY = "volume_permission"


async def persist_source_permission(db, name: str, result: dict | None) -> None:
    """Store a source's verified permission result for later read-only lookup.

    ``result is None`` (the source needs no macOS permission) is stored too, as
    the JSON literal ``null`` — so the UI can tell "checked, nothing needed"
    apart from "never checked" (key absent).
    """
    from estormi_server.storage.tools import write_txn  # noqa: PLC0415

    # Leaf INSERT→commit span — serialised and rollback-guarded. See
    # ``tools.write_txn``.
    async with write_txn():
        await db.execute(
            "INSERT INTO settings(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (_PERMISSION_KEY.format(name=name), json.dumps(result)),
        )


async def reprobe_source_permission(db, name: str) -> dict | None:
    """Re-probe + persist one source's macOS permission after its root changes.

    A folder-rooted source's root (``documents`` / ``code``) is chosen through
    the folder picker, which writes ``<name>_root`` via ``PUT /api/settings`` —
    *not* the source toggle. Only the toggle and launch preflight re-probe, so
    without this the persisted status stays frozen at its toggle-time value
    (typically ``undetermined`` — "choose a folder first", recorded before any
    root existed) and the run gate keeps skipping the stage even after a valid
    folder was picked. Calling this on a ``<name>_root`` write closes that gap:
    it probes the real folder (foreground, attributed to Estormi, since the user
    is in the app) and persists ground truth for the gate to read.

    No-op (returns ``None``) for an unknown source, one that needs no macOS
    permission, or one that isn't enabled — a disabled source must not trigger a
    prompt.
    """
    from connectors import registry  # noqa: PLC0415
    from estormi_server.server.permissions import ensure_source_permission  # noqa: PLC0415

    cls = registry.get(name)
    if cls is None or not cls.spec.macos_permissions:
        return None
    if (await _read_setting(db, f"source_{name}_enabled") or "false").lower() != "true":
        return None
    root = (await _read_setting(db, f"{name}_root")) or None
    result = await asyncio.to_thread(ensure_source_permission, name, root)
    await persist_source_permission(db, name, result)
    return result


async def _enabled_sources_with_roots(db) -> list[tuple[str, str | None]]:
    """Enabled sources that declare a macOS permission, with their root (if any).

    Returns ``(name, root)`` pairs. ``root`` is the configured filesystem root
    for folder-rooted sources (``documents`` / ``code``), used to probe the
    Files-and-Folders / Removable-Volumes prompt against the real folder.
    """
    from connectors import registry  # noqa: PLC0415

    async with db.execute("SELECT key, value FROM settings") as cur:
        settings = {row[0]: row[1] for row in await cur.fetchall()}

    pairs: list[tuple[str, str | None]] = []
    for name in registry.list_all():
        cls = registry.get(name)
        if cls is None or not cls.spec.macos_permissions:
            continue
        if (settings.get(f"source_{name}_enabled", "false") or "false").lower() != "true":
            continue
        pairs.append((name, settings.get(f"{name}_root")))
    return pairs


def _extra_roots(roots: list[str | None]) -> tuple[str, ...]:
    return tuple(r for r in roots if r and r.strip())


async def _configured_extra_paths(db) -> tuple[str, ...]:
    """Operator-supplied paths to fold into the working-set volume probe.

    The ``preflight_extra_paths`` setting (comma / newline / os.pathsep
    separated) lets a power user pre-authorize a removable volume the app
    *indirectly* touches but Estormi's own paths don't reveal — e.g. when the
    briefing's ``claude-cli`` provider stats projects on an external SSD. Empty
    by default, so it's a no-op for a normal install.
    """
    raw = await _read_setting(db, "preflight_extra_paths")
    if not raw:
        return ()
    parts: list[str] = []
    for chunk in raw.replace("\n", ",").replace(os.pathsep, ",").split(","):
        s = chunk.strip()
        if s:
            parts.append(s)
    return tuple(parts)


async def _read_setting(db, key: str) -> str:
    async with db.execute("SELECT value FROM settings WHERE key = ?", (key,)) as cur:
        row = await cur.fetchone()
    return row[0] if row and row[0] else ""


async def run_preflight() -> dict:
    """Probe + persist every enabled source's permission and working-set volumes.

    Returns ``{"sources": [...], "volumes": [...]}`` — the per-source results
    (``ensure_source_permission`` dicts) and one entry per removable volume the
    app reads. Safe on non-macOS: sources report ``unavailable`` and the volume
    list is empty.
    """
    from estormi_server.server.permissions import (  # noqa: PLC0415
        ensure_source_permission,
        probe_working_set_volumes,
    )
    from estormi_server.storage.tools import sqlite_conn, write_txn  # noqa: PLC0415

    db = sqlite_conn()
    pairs = await _enabled_sources_with_roots(db)

    sources: list[dict] = []
    for name, root in pairs:
        try:
            # Sequential: one prompt at a time (see module docstring).
            result = await asyncio.to_thread(ensure_source_permission, name, root)
        except Exception:
            log.exception("preflight_source_probe_failed", source=name)
            # Never fail open into a mid-run prompt: persist a non-authorized
            # status so the run gate skips this stage until the user re-checks,
            # rather than letting the connector trigger a dialog mid-pipeline.
            result = {
                "key": None,
                "label": name,
                "status": "undetermined",
                "detail": "Permission check failed — re-check from the app.",
                "settings_pane": None,
            }
        await persist_source_permission(db, name, result)
        if result is not None:
            sources.append(result)

    extra = _extra_roots([root for _, root in pairs]) + await _configured_extra_paths(db)
    try:
        volumes = await asyncio.to_thread(probe_working_set_volumes, extra)
    except Exception:
        log.exception("preflight_volume_probe_failed")
        volumes = []
    # Leaf multi-row write — hold the write span across the whole loop
    # (persist_source_permission above took it independently per call). See
    # ``tools.write_txn``.
    async with write_txn():
        for vol in volumes:
            await db.execute(
                "INSERT INTO settings(key,value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (f"{_VOLUME_KEY}_{vol['key']}", json.dumps(vol)),
            )

    log.info("preflight_complete", sources=len(sources), volumes=len(volumes))
    return {"sources": sources, "volumes": volumes}
