"""Admin reset endpoints.

Wipe ingested data, settings, or the whole DB. Each endpoint logs a security
decision so the audit log records who triggered the destructive action.
"""

from __future__ import annotations

import asyncio
import glob
import shutil
from pathlib import Path

import httpx
import structlog
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from estormi_server.integrations.whatsapp_sidecar import SIDECAR_URL, sidecar_headers
from estormi_server.server.limiter import limiter
from estormi_server.server.sources import WA_DB_PATH, WA_STAGING_PATH, is_valid_source_slug
from memory_core.audit import log_security_decision

log = structlog.get_logger()

router = APIRouter()


def _purge_dag_logs(logs_dir: Path) -> None:
    """Delete the pipeline run-history log files the pipeline page reads from."""
    for p in (
        [logs_dir / "estormi-daily-dag.log"]
        + [Path(x) for x in glob.glob(str(logs_dir / "estormi-dag-*.log"))]
        + [Path(x) for x in glob.glob(str(logs_dir / "estormi-stage-*.log"))]
    ):
        try:
            p.unlink()
        except OSError:
            pass


def _unlink_audit_log() -> None:
    """Clear the audit log so Recent Searches starts empty after a reset."""
    from memory_core.audit import AUDIT_LOG_PATH  # noqa: PLC0415

    try:
        Path(AUDIT_LOG_PATH).unlink(missing_ok=True)
    except OSError:
        pass


@router.post("/api/sources/{name}/reset")
@limiter.limit("5/minute")
async def reset_source_data(name: str, request: Request):
    """Wipe one source's chunks + vectors + watermark + staging.

    The DangerZone block in SourceManageModal hits this so a "Reset data"
    click on Apple Notes (or any single source) doesn't clobber every other
    source in the vault.

    `name` is the watermark/db key (`notes`, `mail`, `calendar`, `gcal`,
    …) — the same value the watermark-reset endpoint already accepts.
    Validated against a slug pattern before any filesystem or database
    access.
    """
    # Deferred: estormi_server.storage.tools pulls in Qdrant/embedder — kept lazy.
    from estormi_server.storage.tools import (  # noqa: PLC0415
        DATA_DIR,
        get_write_lock,
        sqlite_conn,
    )
    from estormi_server.storage.writers import delete_by_source  # noqa: PLC0415

    if not is_valid_source_slug(name):
        return JSONResponse({"error": f"invalid source: {name!r}"}, status_code=400)

    log_security_decision(
        decision="accept",
        path=f"/api/sources/{name}/reset",
        client_host=request.client.host if request.client else "",
        reason="source_reset_data",
        method="POST",
    )

    # 1) Chunks + Qdrant vectors scoped to this source only.
    result = await delete_by_source(name)

    # 2) Watermark — next run re-derives from the start of the retained window.
    # WhatsApp's watermark is the durable-log key `whatsapp_log` (the `whatsapp`
    # slug carries the derived chunks), so target that one for WhatsApp.
    db = sqlite_conn()
    wm_source = "whatsapp_log" if name == "whatsapp" else name
    # Serialise on the shared write lock so a concurrent leaf writer's commit
    # can't tear this DELETE→commit span. ``delete_by_source`` above already
    # took (and released) the lock itself, so this is a fresh acquisition — not
    # re-entrant. See ``tools._write_lock``.
    async with get_write_lock():
        await db.execute("DELETE FROM ingestion_watermarks WHERE source = ?", (wm_source,))
        await db.commit()

    # 3) Per-source staging dir. WhatsApp is the exception: its raw messages live
    # in the durable `whatsapp_messages` log, and THIS reset only drops the
    # *derived* chunks — they re-derive from the log on the next run with no
    # rescan — so it deliberately leaves the log and any pending staging intact.
    # Wiping the raw log (which forces a rescan) is the separate, heavier action
    # `POST /api/sources/whatsapp/log/reset`.
    if name != "whatsapp":
        staging_root = (Path(DATA_DIR) / "staging").resolve()
        staging = (staging_root / name).resolve()
        # Defence in depth: `name` is already slug-validated above (the pattern
        # admits no path separators), but confirm the resolved target stays
        # strictly inside the staging root before removing it — so any future
        # loosening of the slug rule can't turn this into a traversal.
        if staging.is_relative_to(staging_root) and staging != staging_root and staging.exists():
            shutil.rmtree(staging, ignore_errors=True)

    # 4) Per-source dag_stages history — the IngestionPage's "Last 14 runs"
    # strip is fed by ``/api/pipeline`` which derives each row's slots from
    # ``dag_stages`` filtered by ``stage_name``. Dropping the rows here so
    # the strip is empty (matching the just-wiped chunks/watermark/staging)
    # instead of showing stale green/red squares pointing at deleted data.
    # ``dag_runs`` is engine-scoped — leaving it alone keeps the other
    # sources' history intact.
    async with get_write_lock():
        await db.execute("DELETE FROM dag_stages WHERE stage_name = ?", (name,))
        await db.commit()

    # 5) Per-source stage log files. The canonical naming is
    # ``estormi-stage-<YYYYMMDD-HHMMSS>-<stage>.log`` (one per run); the
    # legacy ``source-<stage>.log`` / ``estormi-stage-<stage>.log`` shapes
    # are kept here for older installs that may still have them.
    logs_dir = Path(DATA_DIR) / "logs"
    targets: list[Path] = [
        logs_dir / f"source-{name}.log",
        logs_dir / f"estormi-stage-{name}.log",
    ]
    targets.extend(Path(p) for p in glob.glob(str(logs_dir / f"estormi-stage-*-{name}.log")))
    for p in targets:
        try:
            p.unlink(missing_ok=True)
        except OSError:
            pass

    return {
        "status": "ok",
        "source": name,
        "chunks_deleted": int(result.get("deleted", 0)),
        "message": f"Cleared {result.get('deleted', 0)} chunks for {name}.",
    }


@router.post("/api/sources/whatsapp/log/reset")
@limiter.limit("5/minute")
async def reset_whatsapp_log(request: Request):
    """Destructive: wipe the durable WhatsApp message log AND its derived chunks.

    Kept separate from ``Reset data`` on purpose — the two have very different
    costs. ``Reset data`` only drops the derived chunks, which re-derive from the
    log on the next run with no rescan. This drops the **raw messages** too, so
    the only way to get them back is a full WhatsApp rescan (the offline queue
    only re-delivers what the server still holds).
    """
    from estormi_server.api.whatsapp_settings import wipe_whatsapp_log  # noqa: PLC0415
    from estormi_server.storage.tools import sqlite_conn  # noqa: PLC0415

    log_security_decision(
        decision="accept",
        path="/api/sources/whatsapp/log/reset",
        client_host=request.client.host if request.client else "",
        reason="whatsapp_log_reset",
        method="POST",
    )

    # Chunks + vectors, raw log, watermark, staging hop, and per-source run
    # history — shared with the Disconnect flow (whatsapp_settings.wipe_whatsapp_log).
    # This "Reset log" keeps the chat list; Disconnect additionally clears it.
    chunks_deleted = await wipe_whatsapp_log(sqlite_conn())

    return {
        "status": "ok",
        "source": "whatsapp",
        "chunks_deleted": chunks_deleted,
        "message": "WhatsApp message log cleared — the next run will rescan from WhatsApp.",
    }


@router.post("/api/admin/reset-settings")
@limiter.limit("5/minute")
async def admin_reset_settings(request: Request):
    from estormi_server.server.jobs import clear_queue, stop_other_engines  # noqa: PLC0415
    from estormi_server.storage.tools import write_txn  # noqa: PLC0415

    # Drain the queue first — same race as the data resets: the queue runner
    # would dispatch a queued entry into the cleared settings table, letting
    # the engine start with a corrupt blank config.
    await clear_queue()
    # An in-flight engine is reading from `settings` (schedule, paths,
    # depth windows, …) and would carry stale config across the wipe.
    # Stop them so the next run picks up the cleared state cleanly.
    await stop_other_engines("reset")

    log_security_decision(
        decision="accept",
        path="/api/admin/reset-settings",
        client_host=request.client.host if request.client else "",
        reason="admin_reset_settings",
        method="POST",
    )
    # Serialised and rollback-guarded leaf write — the identical DELETE in
    # pipeline.py goes through the same span. See ``tools.write_txn``.
    async with write_txn() as db:
        await db.execute("DELETE FROM settings")
    return {"status": "ok", "message": "Settings cleared — reload to continue setup"}


@router.post("/api/admin/reset")
@limiter.limit("5/minute")
async def admin_reset(request: Request):
    from estormi_server.server import events as engine_events  # noqa: PLC0415
    from estormi_server.server.jobs import clear_queue, stop_other_engines  # noqa: PLC0415
    from estormi_server.storage.chunk_admin import reset_db  # noqa: PLC0415
    from estormi_server.storage.tools import DATA_DIR, get_write_lock  # noqa: PLC0415

    log_security_decision(
        decision="accept",
        path="/api/admin/reset",
        client_host=request.client.host if request.client else "",
        reason="admin_reset",
        method="POST",
    )
    # Drain the queue BEFORE stopping the running engine.  Without this the
    # queue runner wakes when the stopped engine's teardown sets the idle
    # event and immediately dispatches the next queued entry into the DB
    # file that reset_db() is concurrently unlinking+recreating → torn writes.
    await clear_queue()
    # Stop every engine first and wait for the bus to confirm idle: ``reset_db``
    # unlinks and recreates estormi.db, so a subprocess mid-INSERT into ``chunks``
    # would corrupt the run or crash on a closed handle.
    await stop_other_engines("reset")
    try:
        await asyncio.wait_for(engine_events.engine_idle_event().wait(), timeout=15.0)
    except asyncio.TimeoutError:
        engine_events.force_clear_current()

    # Delete and recreate the database file (guarantees 0 bytes, bypasses VACUUM
    # quirks). ``reset_db`` closes and reopens the shared connection, so hold the
    # write lock across it: a concurrent leaf writer must not have the connection
    # mid-transaction while it's swapped out. ``reset_db`` doesn't take the lock
    # itself, so this isn't re-entrant. See ``tools._write_lock``.
    async with get_write_lock():
        await reset_db()

    staging = Path(DATA_DIR) / "staging"
    if staging.exists():
        shutil.rmtree(staging, ignore_errors=True)

    # WhatsApp "live staging" lives under the Tauri bundle dir, not DATA_DIR.
    if WA_STAGING_PATH.exists():
        shutil.rmtree(WA_STAGING_PATH, ignore_errors=True)

    _purge_dag_logs(Path(DATA_DIR) / "logs")
    _unlink_audit_log()

    # Delete WhatsApp session database files
    for suffix in ("", "-shm", "-wal"):
        try:
            Path(str(WA_DB_PATH) + suffix).unlink(missing_ok=True)
        except OSError:
            pass

    # Tell the WhatsApp sidecar to reset its in-memory status to UNPAIRED
    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            await client.post(f"{SIDECAR_URL}/api/whatsapp/reset", headers=sidecar_headers())
    except Exception:
        pass  # best-effort: sidecar reset is optional, may be offline

    # ``reset_db`` has already recreated an empty SQLite store. The Qdrant
    # collection MUST be reset too or the vectors orphan; surface a failure
    # instead of swallowing it so the caller learns the stores diverged.
    from estormi_server.storage.qdrant_helpers import ensure_collection  # noqa: PLC0415
    from estormi_server.storage.tools import COLLECTION, _client  # noqa: PLC0415

    try:
        await _client().delete_collection(COLLECTION)
        await ensure_collection()
    except Exception:
        log.exception("reset.qdrant_collection_reset_failed", collection=COLLECTION)
        # Retry once — a transient lock often clears between attempts. If it
        # still fails, raise: SQLite is fresh but the old vectors survive, and
        # the caller needs to know the stores are inconsistent.
        await _client().delete_collection(COLLECTION)
        await ensure_collection()

    # Clear the iCloud companion vault — same reason as the data-only reset:
    # the companion reads only from the vault, so a full reset must wipe it too.
    try:
        from estormi_ingestion.shared.delivery.vault_sync import clear_vault  # noqa: PLC0415

        await asyncio.to_thread(clear_vault)
    except Exception:
        log.exception("reset.vault_clear_failed")

    return {"status": "ok", "message": "All data cleared — reload to continue setup"}
