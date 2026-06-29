"""Queue + engine-run control endpoints.

Estormi's engines (ingestion, briefing, and the optional distill) share a single
in-process FIFO queue in :mod:`server.jobs`. Every launch path — manual buttons and
scheduled cron triggers — funnels through ``enqueue(kind, source)``. A
single queue runner drains the FIFO, one engine at a time. The routes here
are the HTTP surface for that queue:

  - ``POST /api/jobs/queue/clear``   — drop every waiting entry
  - ``POST /api/jobs/queue/remove``  — drop a single waiting entry by kind
  - ``POST /api/jobs/stop``          — kill the currently running engine so
    the next queued entry launches
  - ``GET  /api/jobs/state``         — snapshot for REST callers (the SPA
    normally consumes ``/api/events`` SSE instead)
  - ``POST /api/jobs/wake-catchup``  — run any cron launches missed while the
    Mac was asleep (called by the Tauri shell on wake)

Process state lives in ``server.jobs``; this module only reads from it.
"""

from __future__ import annotations

import structlog
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from estormi_server.server import events as engine_events
from estormi_server.server import jobs as job_state
from estormi_server.server.limiter import limiter

log = structlog.get_logger()

router = APIRouter()


# `Literal[...]` would be tighter, but the runtime check below already
# rejects unknown kinds with a clear 400 — keep this a string for
# pydantic's friendlier coercion.
_VALID_KINDS = {"ingestion", "briefing", "distill"}


@router.post("/api/jobs/queue/clear")
@limiter.limit("10/minute")
async def api_jobs_queue_clear(request: Request):
    """Drain the queue. The currently running engine is left alone."""
    dropped = await job_state.clear_queue()
    return {"status": "cleared", "dropped": dropped, "queue": job_state.queue_snapshot()}


class RemoveBody(BaseModel):
    kind: str = Field(..., description="engine to drop from the waiting queue")


@router.post("/api/jobs/queue/remove")
@limiter.limit("30/minute")
async def api_jobs_queue_remove(request: Request, body: RemoveBody):
    """Drop a single waiting entry. The running engine is left alone."""
    if body.kind not in _VALID_KINDS:
        return JSONResponse({"error": f"unknown kind: {body.kind!r}"}, status_code=400)
    removed = await job_state.remove_from_queue(body.kind)  # type: ignore[arg-type]
    return {
        "status": "removed" if removed else "not_queued",
        "queue": job_state.queue_snapshot(),
    }


class StopBody(BaseModel):
    kind: str = Field(..., description="engine currently running to stop")


@router.post("/api/jobs/stop")
@limiter.limit("10/minute")
async def api_jobs_stop(request: Request, body: StopBody):
    """Kill the running engine so the next queued entry can launch.

    No-op when ``kind`` isn't actually running — guards against a stale
    UI click after SSE has already moved on. The launcher's
    ``_close_log_on_exit`` task fires ``emit_stopped`` once the
    subprocess exits, which is what the queue runner waits on.
    """
    if body.kind not in _VALID_KINDS:
        return JSONResponse({"error": f"unknown kind: {body.kind!r}"}, status_code=400)
    current = engine_events.current_kind()
    if current != body.kind:
        return {"status": "not_running", "running": current}
    await job_state.stop_engine(body.kind)  # type: ignore[arg-type]
    return {"status": "stopped"}


@router.post("/api/jobs/wake-catchup")
@limiter.limit("12/minute")
async def api_jobs_wake_catchup(request: Request):
    """Run after a system wake: enqueue any scheduled engine run missed while
    the Mac slept (the in-process scheduler can't fire during sleep). The Tauri
    shell calls this when its health loop detects a wall-clock jump. Idempotent
    — ``enqueue`` dedupes by kind, so calling it spuriously is harmless."""
    enqueued = await job_state.wake_catchup()
    return {"status": "ok", "enqueued": enqueued, "queue": job_state.queue_snapshot()}


@router.get("/api/jobs/state")
@limiter.limit("60/minute")
async def api_jobs_state(request: Request):
    """Snapshot of the engine room — running kind, queue, and the queue
    runner's view of "what's mine right now"."""
    current = engine_events.current_kind()
    return {
        "running": current,
        "queue": job_state.queue_snapshot(),
    }


@router.get("/api/jobs/schedule")
@limiter.limit("60/minute")
async def api_jobs_schedule(request: Request):
    """Upcoming automatic launches, for the engine room's UPCOMING section.

    The two daily crons (ingestion / briefing) report their APScheduler
    ``next_run_time``; the WHOOP wake trigger reports its window and whether
    it already fired today (it enqueues a ~1-minute readiness refresh when
    the briefing exists — see ``server.schedulers._schedule_whoop_poll``).
    """
    from estormi_server.sql.connection import _get_setting  # noqa: PLC0415

    def _next_fire(job_id: str) -> str | None:
        job = job_state._scheduler.get_job(job_id)
        when = getattr(job, "next_run_time", None) if job else None
        return when.isoformat() if when else None

    return {
        "crons": [
            {"kind": "ingestion", "nextRun": _next_fire("daily_dag")},
            {"kind": "briefing", "nextRun": _next_fire("daily_briefing")},
        ],
        "whoopWake": {
            "enabled": (await _get_setting("whoop_polling_enabled", "false")) == "true",
            "windowStartHour": int(await _get_setting("whoop_polling_window_start_hour", "5")),
            "windowEndHour": int(await _get_setting("whoop_polling_window_end_hour", "11")),
            "lastFiredDate": await _get_setting(
                "whoop_polling_last_fired_date", "", env_override=False
            ),
            "nextCheck": _next_fire(job_state._WHOOP_POLL_JOB_ID),
        },
    }
