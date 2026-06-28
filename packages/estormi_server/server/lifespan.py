"""FastAPI lifespan handler for Estormi.

Encapsulates one-time migrations, SQLite bootstrap, Qdrant collection
creation, scheduler boot, and the corresponding shutdown teardown. The
order of operations is load-bearing — moving any step would change either
crash semantics on first launch or the behaviour observed by integration
tests that rely on ``ensure_collection`` being patched before startup.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import os
from contextlib import asynccontextmanager

import aiosqlite
import structlog
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from fastapi import FastAPI

from estormi_server.services.pipeline_status import (
    ENGINE_SCHEDULE_DEFAULTS,
    set_schedule_cron,
)
from estormi_server.sql.connection import _get_setting
from estormi_server.sql.schema import INIT_SQL, MIGRATION_SQL, _apply_chunk_column_migrations
from estormi_server.storage import tools
from estormi_server.storage.qdrant_helpers import ensure_collection

from . import jobs
from .jobs import (
    _kill_briefing,
    _queue_runner,
    _schedule_briefing,
    _schedule_distill,
    _schedule_ingestion,
    _scheduler,
    apply_whoop_polling_schedule,
)
from .launchers.distill import _kill_distill

log = structlog.get_logger()


def _ingestion_catchup_stale(last, now) -> bool:
    """True when the most-recent ingestion run warrants a startup catch-up.

    A ``cancelled`` most-recent run means the previous pipeline crashed mid-flight
    (``reconcile_orphaned_runs`` marks orphans cancelled). Its ``started_at`` is
    recent, so an age-only check would treat it as fresh and skip the very
    catch-up it needs — force it stale. Also stale when there's no prior run or
    the last one is over 24h old.
    """
    if last is None:
        return True
    if last.status == "cancelled":
        return True
    return (now - last.started_at).total_seconds() > 24 * 3600


async def _backfill_chat_kind_payloads() -> None:
    """Move the structural WhatsApp kind out of the Qdrant `group_type` payload.

    Legacy chunks stored the JID fallback (dm/group/broadcast) in `group_type`;
    that axis now lives in `chat_kind`. Three filtered `set_payload` calls move
    it across and reset `group_type` to 'unknown'. Naturally idempotent — once a
    point's `group_type` is no longer structural the matching filter is empty —
    and gated by a one-shot settings flag so it's skipped on subsequent boots.
    Only the previously-uncategorised chats (those that carried a structural
    `group_type`) are touched; semantically-tagged WhatsApp chunks pick up
    `chat_kind` on their next re-ingest.
    """
    from qdrant_client.models import FieldCondition, Filter, MatchValue  # noqa: PLC0415

    if (await _get_setting("chat_kind_backfilled", "") or "").strip() == "1":
        return
    client = tools._client()
    for kind in ("group", "dm", "broadcast"):
        await client.set_payload(
            collection_name=tools.COLLECTION,
            payload={"chat_kind": kind, "group_type": "unknown"},
            points=Filter(must=[FieldCondition(key="group_type", match=MatchValue(value=kind))]),
        )
    db = tools.sqlite_conn()
    # Leaf INSERT→commit span — serialise on the shared write lock. See
    # ``tools._write_lock``.
    async with tools.get_write_lock():
        await db.execute(
            "INSERT INTO settings (key, value) VALUES ('chat_kind_backfilled', '1') "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value"
        )
        await db.commit()
    log.info("qdrant.chat_kind_backfill_done")


async def _bootstrap_sqlite() -> None:
    """Open, migrate, and configure the chunk-store database."""
    tools._db = await aiosqlite.connect(tools.DB_PATH)
    try:
        os.chmod(tools.DB_PATH, 0o600)
    except OSError:
        pass
    tools._db.row_factory = aiosqlite.Row
    # These three PRAGMAs MUST stay in lock-step with the synchronous sqlite3
    # connections that `memory_core.dag_state._connect()` opens against the same
    # DB file (it sets the identical WAL / busy_timeout=30000 / foreign_keys=ON).
    await tools._db.execute("PRAGMA journal_mode=WAL")
    await tools._db.execute("PRAGMA busy_timeout=30000")
    await tools._db.execute("PRAGMA foreign_keys=ON")
    await tools._db.executescript(INIT_SQL)
    await _apply_chunk_column_migrations(tools._db)
    await tools._db.executescript(MIGRATION_SQL)
    await tools._db.commit()
    from memory_core.dag_state import ensure_schema as _ensure_dag_state_schema  # noqa: PLC0415
    from memory_core.engine_lock import ensure_schema as _ensure_engine_lock_schema  # noqa: PLC0415

    await _ensure_dag_state_schema(tools._db)
    await _ensure_engine_lock_schema(tools._db)
    _cur = await tools._db.execute("SELECT value FROM settings WHERE key = 'embed_model'")
    _row = await _cur.fetchone()
    await _cur.close()
    if _row and str(_row[0]).startswith("text-embedding-"):
        async with tools.get_write_lock():
            await tools._db.execute(
                "INSERT INTO settings (key, value) VALUES ('embed_model', ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                ("nomic-ai/nomic-embed-text-v1.5",),
            )
            await tools._db.commit()


async def _bootstrap_qdrant() -> None:
    """Ensure the Qdrant collection exists and run one-off payload migrations."""
    try:
        await ensure_collection()
        tools._collection_ready = True
    except Exception as _qdrant_err:
        log.warning("qdrant.locked_at_startup", error=str(_qdrant_err), exc_info=True)

    if tools._collection_ready:
        try:
            await _backfill_chat_kind_payloads()
        except Exception as _ck_err:
            log.warning("qdrant.chat_kind_backfill_failed", error=str(_ck_err), exc_info=True)


def _reconcile_orphaned_engines() -> None:
    """Mark leftover ``running`` dag_runs as cancelled when no live engine holds the lock."""
    from memory_core import dag_state, engine_lock  # noqa: PLC0415

    _lock = engine_lock.current()
    ingestion_alive = (
        _lock is not None and _lock.kind == "ingestion" and engine_lock.is_alive(_lock)
    )
    dag_state.reconcile_orphaned_runs(pid_file_exists=ingestion_alive, engine="ingestion")
    dag_state.reconcile_orphaned_runs(pid_file_exists=False, engine="briefing")
    distill_alive = (
        _lock is not None and _lock.kind == "distill" and engine_lock.is_alive(_lock)
    )
    dag_state.reconcile_orphaned_runs(pid_file_exists=distill_alive, engine="distill")


async def _register_engine_schedules() -> str:
    """Register APScheduler cron jobs for the three engines. Returns the ingestion schedule."""
    schedule = await _get_setting("schedule_cron", ENGINE_SCHEDULE_DEFAULTS["schedule_cron"])
    set_schedule_cron(schedule)
    if schedule != "manual":
        _scheduler.add_job(
            _schedule_ingestion,
            CronTrigger.from_crontab(schedule),
            id="daily_dag",
        )

    briefing_schedule = await _get_setting(
        "briefing_schedule_cron", ENGINE_SCHEDULE_DEFAULTS["briefing_schedule_cron"]
    )
    if briefing_schedule != "manual":
        _scheduler.add_job(
            _schedule_briefing,
            CronTrigger.from_crontab(briefing_schedule),
            id="daily_briefing",
        )

    distill_schedule = await _get_setting(
        "distill_schedule_cron", ENGINE_SCHEDULE_DEFAULTS["distill_schedule_cron"]
    )
    if distill_schedule != "manual":
        _scheduler.add_job(
            _schedule_distill,
            CronTrigger.from_crontab(distill_schedule),
            id="weekly_distill",
        )

    await apply_whoop_polling_schedule()

    _scheduler.add_job(
        tools.heal_orphaned_write_txn,
        IntervalTrigger(seconds=60),
        id="db_txn_watchdog",
    )

    _scheduler.start()
    return schedule


@asynccontextmanager
async def lifespan(app: FastAPI):
    _estormi_executor = concurrent.futures.ThreadPoolExecutor(
        max_workers=8, thread_name_prefix="estormi-pool"
    )
    asyncio.get_running_loop().set_default_executor(_estormi_executor)

    try:
        from estormi_server.server.security import refresh_token_cache  # noqa: PLC0415

        await refresh_token_cache()
    except Exception as _tok_err:  # pragma: no cover — defensive
        log.warning("security.token_cache_prime_failed", error=str(_tok_err))

    from memory_core.settings import ensure_private_dir  # noqa: PLC0415

    ensure_private_dir(tools.DATA_DIR)

    try:
        from estormi_ingestion.shared.delivery.cloudkit_doorbell import (  # noqa: PLC0415
            migrate_helper_to_config_home,
        )

        await asyncio.to_thread(migrate_helper_to_config_home)
    except Exception:  # noqa: BLE001 — defensive; the resolver still finds a legacy install
        log.warning("doorbell.helper_migration_skipped", exc_info=True)

    await _bootstrap_sqlite()
    await _bootstrap_qdrant()

    try:
        _reconcile_orphaned_engines()
    except Exception as _orphan_err:
        log.warning("dag_state.reconcile.error", error=str(_orphan_err), exc_info=True)

    schedule = await _register_engine_schedules()

    # Single in-process queue runner — drains `server.jobs._queue` FIFO,
    # one engine at a time. Manual buttons, scheduled triggers, and backlog
    # watchers all funnel through `jobs.enqueue(...)`; this task is the
    # only thing that actually invokes the launcher coroutines.
    #
    # The runner already self-restarts its inner loop on exception, but if
    # the task object itself ends for any non-shutdown reason (GC, an
    # exception thrown outside the wrapped block, a future refactor that
    # introduces a return path), this watchdog respawns it so the queue
    # never goes silent.
    _runner_holder: dict[str, asyncio.Task] = {}
    _runner_shutdown = False

    def _spawn_runner() -> None:
        task = asyncio.create_task(_queue_runner(), name="queue_runner")
        _runner_holder["task"] = task

        def _on_done(t: asyncio.Task) -> None:
            if _runner_shutdown or t.cancelled():
                return
            log.error(
                "queue_runner.task_died_respawning",
                exception=t.exception(),
            )
            # Re-prime the wakeup event so the new runner picks up any
            # pending queue entries even if no fresh enqueue follows.
            jobs._queue_changed.set()
            _spawn_runner()

        task.add_done_callback(_on_done)

    _spawn_runner()

    # Group every macOS permission prompt at launch, then catch up a missed
    # pipeline run — both as one background task so neither blocks server boot, and so
    # the catch-up only enqueues *after* the preflight has (re)granted access.
    # The APScheduler cron fires the routine run; this catch-up covers the case
    # where the app wasn't running at the scheduled cron time (an in-process
    # scheduler can't fire while the app is down).
    async def _preflight_then_catchup() -> None:
        try:
            from estormi_server.server.permission_preflight import run_preflight  # noqa: PLC0415

            await run_preflight()
        except Exception:
            log.warning("startup.preflight_failed", exc_info=True)
        if schedule == "manual":
            return
        try:
            from datetime import datetime, timezone  # noqa: PLC0415

            from memory_core import dag_state  # noqa: PLC0415

            recent = dag_state.get_recent_runs(limit=1, engine="ingestion")
            last = recent[0] if recent else None
            stale = _ingestion_catchup_stale(last, datetime.now(timezone.utc))
            if stale:
                log.info("startup.dag_catchup_enqueue", last_run=bool(last))
                await jobs.enqueue("ingestion", "schedule")
        except Exception:
            log.warning("startup.dag_catchup_failed", exc_info=True)

    jobs._track_background_task(asyncio.create_task(_preflight_then_catchup()))

    async def _prewarm_embeddings() -> None:
        try:
            from memory_core.embedder import embed_one, sparse_embed_one  # noqa: PLC0415

            await embed_one("warmup")
            await sparse_embed_one("warmup")
            log.info("startup.embeddings_prewarmed")
        except Exception:
            log.warning("startup.embeddings_prewarm_failed", exc_info=True)

    jobs._track_background_task(asyncio.create_task(_prewarm_embeddings()))

    log.info("startup.ready")
    yield

    _runner_shutdown = True
    _runner_task = _runner_holder.get("task")
    if _runner_task is not None:
        _runner_task.cancel()
        try:
            await _runner_task
        except (asyncio.CancelledError, Exception):
            pass
    _scheduler.shutdown(wait=False)
    await _kill_briefing()
    await _kill_distill()
    # Use the same SIGTERM → SIGKILL escalation as runtime kills. The raw
    # killpg(SIGTERM) call this replaced let a wedged pipeline worker outlive
    # lifespan shutdown — _kill_dag_processes now waits up to ~3 s and
    # force-kills before clearing the pid file.
    await jobs._kill_dag_processes()
    # Drain background tasks (per-engine snapshot pushes, log-close watchers
    # registered via jobs._background_tasks) before tearing the DB down, so a
    # late vault snapshot doesn't try to read from a closed sqlite.
    pending = list(jobs._background_tasks)
    for task in pending:
        task.cancel()
    if pending:
        try:
            await asyncio.wait(pending, timeout=5)
        except Exception:
            pass
    await tools._db.close()
    tools._db = None
    # Shut the replacement executor down so repeated lifespan cycles in one
    # process (the integration suite) don't leak up to 64 "estormi-pool"
    # threads per cycle. Don't wait — outstanding work is best-effort at exit.
    _estormi_executor.shutdown(wait=False)
