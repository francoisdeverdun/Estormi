"""``/api/settings`` GET and PUT.

GET returns the entire settings table as a flat ``{key: value}`` map.
PUT upserts each ``key: value`` pair from the JSON body and — as a side
effect — reschedules the daily ingestion pipeline when its trigger key (``schedule_cron``)
changes. The validation matrix (credential / server-managed keys, caps, cron
syntax, WHOOP poller bounds, language / TTS-voice enums) lives in
:mod:`estormi_server.services.settings`; this handler only maps a rejection to
the right status code and then performs the upsert + scheduler side effects.
Every accepted PUT emits an ``accept`` security-audit entry listing the keys
that were written.
"""

from __future__ import annotations

import structlog
from apscheduler.triggers.cron import CronTrigger
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from estormi_server.server.jobs import (
    _schedule_briefing,
    _schedule_distill,
    _schedule_ingestion,
    _scheduler,
    apply_whoop_polling_schedule,
)
from estormi_server.server.limiter import limiter
from estormi_server.services.pipeline_status import set_schedule_cron
from estormi_server.services.settings import validate_settings_update
from estormi_server.storage.tools import get_write_lock, sqlite_conn

log = structlog.get_logger()

router = APIRouter()


@router.get("/api/settings")
@limiter.limit("30/minute")
async def get_settings(request: Request):
    db = sqlite_conn()
    cursor = await db.execute("SELECT key, value FROM settings")
    rows = await cursor.fetchall()
    await cursor.close()
    return {row["key"]: row["value"] for row in rows}


@router.put("/api/settings")
@limiter.limit("30/minute")
async def put_settings(request: Request, updates: dict[str, str]):
    from memory_core.audit import log_security_decision  # noqa: PLC0415

    error = validate_settings_update(updates)
    if error is not None:
        return JSONResponse({"error": error.message}, status_code=error.status_code)

    log_security_decision(
        decision="accept",
        path="/api/settings",
        client_host=request.client.host if request.client else "",
        reason="settings_write:" + ",".join(sorted(updates.keys()))[:200],
        method="PUT",
    )
    db = sqlite_conn()
    # Multi-row write — hold the shared write lock across the whole loop+commit
    # so a concurrent leaf writer's commit can't flush a half-applied batch.
    # Leaf (apply_whoop_polling_schedule below runs after the lock is released).
    # See ``tools._write_lock``.
    async with get_write_lock():
        for key, value in updates.items():
            await db.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )
        await db.commit()

    if "schedule_cron" in updates:
        new_schedule = updates["schedule_cron"]
        set_schedule_cron(new_schedule)
        if new_schedule == "manual":
            if _scheduler.get_job("daily_dag"):
                _scheduler.remove_job("daily_dag")
        else:
            trigger = CronTrigger.from_crontab(new_schedule)
            if _scheduler.get_job("daily_dag"):
                _scheduler.reschedule_job("daily_dag", trigger=trigger)
            else:
                _scheduler.add_job(_schedule_ingestion, trigger, id="daily_dag")

    # Briefing cron — mirror the daily_dag dance so a UI change takes effect
    # without restarting the server.
    if "briefing_schedule_cron" in updates:
        new_briefing = updates["briefing_schedule_cron"]
        if new_briefing == "manual":
            if _scheduler.get_job("daily_briefing"):
                _scheduler.remove_job("daily_briefing")
        else:
            trigger = CronTrigger.from_crontab(new_briefing)
            if _scheduler.get_job("daily_briefing"):
                _scheduler.reschedule_job("daily_briefing", trigger=trigger)
            else:
                _scheduler.add_job(_schedule_briefing, trigger, id="daily_briefing")

    # Distillation cron — same dance; weekly retrain of the local quill.
    if "distill_schedule_cron" in updates:
        new_distill = updates["distill_schedule_cron"]
        if new_distill == "manual":
            if _scheduler.get_job("weekly_distill"):
                _scheduler.remove_job("weekly_distill")
        else:
            trigger = CronTrigger.from_crontab(new_distill)
            if _scheduler.get_job("weekly_distill"):
                _scheduler.reschedule_job("weekly_distill", trigger=trigger)
            else:
                _scheduler.add_job(_schedule_distill, trigger, id="weekly_distill")

    # WHOOP poller — re-apply on any of its knobs changing so a toggle or a
    # new interval takes effect live (the helper adds / reschedules / removes
    # the `whoop_polling` job to match current settings).
    if any(k.startswith("whoop_polling_") for k in updates):
        await apply_whoop_polling_schedule()

    # A folder-rooted source's root (`documents_root`, `code_root`) is written
    # here by the folder picker — not the source toggle. Re-probe its macOS
    # permission against the freshly-picked folder, or the run gate keeps reading
    # the stale toggle-time status and skips the stage. See
    # server/permission_preflight.reprobe_source_permission.
    root_changes = [k for k in updates if k.endswith("_root")]
    if root_changes:
        from estormi_server.server.permission_preflight import reprobe_source_permission

        for key in root_changes:
            name = key[: -len("_root")]
            try:
                await reprobe_source_permission(db, name)
            except Exception:
                log.exception("settings_reprobe_failed", source=name)

    return {k: v for k, v in updates.items()}
