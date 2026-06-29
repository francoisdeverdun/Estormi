"""Service layer behind the ``/api/knowledge`` + ``/api/briefings`` routers.

The router under :mod:`estormi_server.api.knowledge` owns HTTP concerns only
(route decorators, rate limiting, request/response shaping). The SQL reads /
writes against the ``settings`` and ``briefing_runs`` tables and the
briefings-reset choreography (stop the engine, clear the tables, wipe the
vault) live here so they are unit-testable without an ASGI client.

All ``tools`` / ``jobs`` references are late-bound inside the functions so the
test suite's ``patch("estormi_server.storage.tools…")`` / ``patch("estormi_server.server.jobs…")``
hooks keep resolving against the source modules.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import structlog

log = structlog.get_logger()


def parse_json_col(raw: object) -> object:
    """Decode a JSON text column with a best-effort fallback to ``{}``.

    Briefing rows may pre-date the JSON columns (NULL), and a corrupt blob
    shouldn't 500 the whole runs endpoint.
    """
    if raw is None or raw == "":
        return {}
    try:
        return json.loads(str(raw))
    except (TypeError, ValueError):
        return {}


async def knowledge_status() -> dict:
    """Last-run state for the briefing engine plus a ``running`` flag.

    Reads the three ``knowledge_last_run_*`` settings rows and folds in the
    live engine state from ``server.jobs``.
    """
    from estormi_server.server import jobs  # noqa: PLC0415
    from estormi_server.storage.tools import sqlite_conn  # noqa: PLC0415

    db = sqlite_conn()
    keys = [
        "knowledge_last_run_at",
        "knowledge_last_run_status",
        "knowledge_last_run_summary",
    ]
    result: dict = {}
    for key in keys:
        cur = await db.execute("SELECT value FROM settings WHERE key = ?", (key,))
        row = await cur.fetchone()
        await cur.close()
        result[key] = row[0] if row else ""
    result["running"] = await jobs._briefing_running()
    return result


async def stop_briefing() -> dict:
    """Drop a queued briefing, kill any in-flight run, and record the stop.

    Returns ``{"status": "not_running"}`` when nothing was running, otherwise
    ``{"status": "stopped"}`` after persisting ``knowledge_last_run_status``.
    """
    from estormi_server.server import jobs  # noqa: PLC0415
    from estormi_server.storage.tools import sqlite_conn  # noqa: PLC0415

    # Drop any waiting briefing entry first so the queue runner doesn't
    # re-dispatch into the trailing ``pkill -f`` straggler-catch.
    await jobs.remove_from_queue("briefing")
    if not await jobs._briefing_running():
        return {"status": "not_running"}
    await jobs._kill_briefing()
    db = sqlite_conn()
    # Leaf INSERT→commit span — serialise on the shared write lock. See
    # ``tools._write_lock``.
    from estormi_server.storage.tools import get_write_lock  # noqa: PLC0415

    async with get_write_lock():
        await db.execute(
            "INSERT INTO settings (key, value) VALUES ('knowledge_last_run_status', 'stopped') "
            "ON CONFLICT(key) DO UPDATE SET value = 'stopped'"
        )
        await db.commit()
    return {"status": "stopped"}


async def recent_runs(days: int, limit: int) -> list[dict]:
    """Recent ``briefing_runs`` rows, newest-first, within ``days`` days.

    ``limit`` caps the row count regardless of the window. ``sections`` is
    decoded from the row's JSON column via :func:`parse_json_col`.
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    from estormi_server.storage.tools import sqlite_conn  # noqa: PLC0415

    db = sqlite_conn()
    cur = await db.execute(
        """
        SELECT id, started_at, finished_at, status, duration_ms, model,
               tokens_in, tokens_out, sections_json,
               items_considered, items_included,
               summary
        FROM briefing_runs
        WHERE started_at >= ?
        ORDER BY started_at DESC
        LIMIT ?
        """,
        (cutoff, limit),
    )
    rows = await cur.fetchall()
    await cur.close()
    out: list[dict] = []
    for r in rows:
        out.append(
            {
                "id": r["id"],
                "started_at": r["started_at"],
                "finished_at": r["finished_at"],
                "status": r["status"],
                "duration_ms": r["duration_ms"],
                "model": r["model"],
                "tokens_in": r["tokens_in"],
                "tokens_out": r["tokens_out"],
                "sections": parse_json_col(r["sections_json"]),
                "items_considered": r["items_considered"],
                "items_included": r["items_included"],
                "summary": r["summary"],
            }
        )
    return out


async def reset_briefings() -> dict:
    """Wipe every composed briefing plus the engine's run history.

    Stops any in-flight briefing run (waiting for the engine to go idle so we
    don't race a mid-INSERT subprocess), then clears the legacy briefing-source
    chunks, the ``briefing_runs`` / ``dag_runs`` history, the last-run settings,
    the vault briefings folder, and the rolling ``knowledge.log``.

    Returns ``{"status", "chunks_deleted", "vault_files_deleted"}``.
    """
    import asyncio  # noqa: PLC0415
    import os  # noqa: PLC0415

    from estormi_server.server import events as engine_events  # noqa: PLC0415
    from estormi_server.server import jobs  # noqa: PLC0415
    from estormi_server.storage.tools import sqlite_conn  # noqa: PLC0415
    from estormi_server.storage.writers import delete_by_source  # noqa: PLC0415

    # Stop any in-flight briefing run so it cannot keep writing into the
    # tables we are about to clear. ``_kill_briefing`` only sends SIGTERM
    # and returns immediately; the subprocess can still be mid-INSERT for a
    # few seconds while its ``_close_log_on_exit`` watcher waits on
    # ``proc.wait()`` to emit ``stopped``. Wait for that idle signal before
    # TRUNCATing the tables — otherwise we race the engine and lose data
    # the briefing was about to commit (or worse, write back into the
    # cleared epoch after we return).
    await jobs.remove_from_queue("briefing")
    if await jobs._briefing_running():
        await jobs._kill_briefing()
        try:
            await asyncio.wait_for(engine_events.engine_idle_event().wait(), timeout=15.0)
        except asyncio.TimeoutError:
            # The briefing subprocess outlived its grace window. Force the
            # event bus back to idle so we don't deadlock here; the kill
            # signal stays in flight and the SIGKILL fallback in
            # ``_kill_briefing`` will finish the job. Logged so the user
            # can see why the reset took longer than usual.
            engine_events.force_clear_current()

    # 1) Any legacy briefing-source chunks + their Qdrant vectors. Current
    # briefings are vault-only, so this is normally a no-op (see docstring).
    chunks_result = await delete_by_source("briefing")

    # 2) Engine-run history (the briefing_runs feed and the dag_runs strip
    # for this engine) plus the "last run" settings that drive the knowledge
    # status banner — all three belong to the cleared epoch.
    db = sqlite_conn()
    # delete_by_source above already took (and released) the write lock itself;
    # this fresh acquisition serialises the run-history DELETE→commit span so a
    # concurrent leaf writer's commit can't tear it. Not re-entrant. See
    # ``tools._write_lock``.
    from estormi_server.storage.tools import get_write_lock  # noqa: PLC0415

    async with get_write_lock():
        await db.execute("DELETE FROM briefing_runs")
        await db.execute("DELETE FROM dag_runs WHERE engine = 'briefing'")
        await db.execute(
            "DELETE FROM settings WHERE key IN "
            "('knowledge_last_run_at','knowledge_last_run_status','knowledge_last_run_summary')"
        )
        await db.commit()

    # 3) Vault briefings folder — drop every per-day JSON and refresh the
    # manifest so the iOS companion sees the reset on its next foreground.
    from estormi_ingestion.shared.delivery import vault_sync  # noqa: PLC0415

    def _wipe_vault_briefings() -> int:
        deleted = 0
        try:
            d = vault_sync.vault_dir()
            briefings_dir = d / "briefings"
            if briefings_dir.is_dir():
                for p in briefings_dir.glob("*.json"):
                    try:
                        p.unlink()
                        deleted += 1
                    except OSError:
                        pass
            if d.is_dir():
                vault_sync._rebuild_manifest(d)
        except Exception:  # pragma: no cover — defensive
            log.exception("briefings_reset.vault_cleanup_failed")
        return deleted

    vault_files_deleted = await asyncio.to_thread(_wipe_vault_briefings)

    # 4) Rolling knowledge.log — engine history is gone, so the log tail
    # should reset too rather than show stale lines from cleared runs.
    try:
        os.unlink(jobs._KNOWLEDGE_LOG)
    except FileNotFoundError:
        pass
    except OSError:
        pass

    return {
        "status": "ok",
        "chunks_deleted": int(chunks_result.get("deleted", 0)),
        "vault_files_deleted": vault_files_deleted,
    }
