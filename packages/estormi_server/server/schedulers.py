"""Scheduler-driven engine triggers layered on the FIFO queue.

The engine mutex, the FIFO run queue, and the queue runner live in
``server.jobs``. This module owns the *scheduling* concerns that decide *when*
to enqueue — kept separate so ``jobs.py`` stays focused on the queue + runner:

* the system-wake catch-up (``wake_catchup`` + ``_most_recent_fire``), which
  re-enqueues a scheduled engine whose cron fire was missed while the Mac slept;
* the WHOOP morning poller (``_schedule_whoop_poll`` /
  ``apply_whoop_polling_schedule``), which fires the pipeline once WHOOP has
  scored the night's recovery.

Both funnel through ``jobs.enqueue`` — they never spawn engines directly.

These functions deliberately reach the shared scheduler state (``_scheduler``,
``enqueue``, ``datetime``, ``IntervalTrigger``, ``log``) through the ``_jobs``
module object rather than importing the names directly, so the existing test
patches on ``server.jobs.<name>`` (e.g. ``patch("…server.jobs.enqueue")``,
``patch("…server.jobs.datetime")``) keep driving this code. ``jobs.py``
re-exports every public name here so ``from server.jobs import
apply_whoop_polling_schedule`` and ``jobs.wake_catchup`` continue to resolve.
"""

from __future__ import annotations

import asyncio
from datetime import timezone

from estormi_server.server import jobs as _jobs
from estormi_server.server.events import EngineKind
from estormi_server.services.pipeline_status import ENGINE_SCHEDULE_DEFAULTS

# ─── system-wake catch-up ────────────────────────────────────────────────────
# The in-process scheduler can't fire while the Mac is asleep, so a cron whose
# time falls during sleep is simply missed (APScheduler's misfire grace is one
# second). The Tauri shell detects wake — a wall-clock jump in its health loop —
# and calls /api/jobs/wake-catchup, which re-enqueues any scheduled engine whose
# most-recent fire was missed. enqueue() dedupes by kind, so a double trigger
# (a cron that did fire + the wake call) can't pile up.

# (engine, settings key, default cron) — defaults from the shared
# ENGINE_SCHEDULE_DEFAULTS. distill is intentionally absent: wake catch-up
# covers only the daily engines (the heavy weekly retrain is not re-enqueued
# on wake).
_CATCHUP_ENGINES: tuple[tuple[EngineKind, str, str], ...] = (
    ("ingestion", "schedule_cron", ENGINE_SCHEDULE_DEFAULTS["schedule_cron"]),
    ("briefing", "briefing_schedule_cron", ENGINE_SCHEDULE_DEFAULTS["briefing_schedule_cron"]),
)


def _most_recent_fire(trigger, now):
    """Latest scheduled fire time at or before ``now`` within the last ~25h,
    or ``None`` if the cron didn't fire in that window."""
    from datetime import timedelta  # noqa: PLC0415

    prev = None
    fire = trigger.get_next_fire_time(None, now - timedelta(hours=25))
    while fire is not None and fire <= now:
        prev = fire
        fire = trigger.get_next_fire_time(fire, fire + timedelta(seconds=1))
    return prev


async def wake_catchup(now=None, tz=None) -> list[str]:
    """Enqueue any scheduled engine run missed while the Mac slept.

    For each engine, find its most recent scheduled fire in the last day; if no
    run started at/after that fire, the cron was missed during sleep — enqueue a
    catch-up. Returns the engines enqueued. Best-effort: a malformed cron or
    bookkeeping error for one engine never blocks the others or the wake path.

    ``now``/``tz`` are injectable for tests; in production both come from the
    scheduler so the cron maths uses the same timezone the jobs run in.
    """
    from apscheduler.triggers.cron import CronTrigger  # noqa: PLC0415

    from estormi_server.sql.connection import _get_setting  # noqa: PLC0415
    from memory_core import dag_state  # noqa: PLC0415

    if tz is None:
        tz = getattr(_jobs._scheduler, "timezone", None)
    if now is None:
        now = _jobs.datetime.now(tz) if tz is not None else _jobs.datetime.now(timezone.utc)
    enqueued: list[str] = []
    for engine, key, default in _CATCHUP_ENGINES:
        try:
            cron = await _get_setting(key, default)
            if not cron or cron == "manual":
                continue
            trigger = CronTrigger.from_crontab(cron, timezone=tz)
            last_fire = _most_recent_fire(trigger, now)
            if last_fire is None:
                continue
            # ``get_recent_runs`` is synchronous sqlite; off-load it so the probe
            # doesn't block the event loop while reading run history.
            recent = await asyncio.to_thread(dag_state.get_recent_runs, limit=1, engine=engine)
            last = recent[0] if recent else None
            last_started = last.started_at if last else None
            if last_started is not None and last_started.tzinfo is None:
                last_started = last_started.replace(tzinfo=timezone.utc)
            if last_started is None or last_started < last_fire:
                _jobs.log.info(
                    "wake_catchup.enqueue", engine=engine, missed_fire=last_fire.isoformat()
                )
                await _jobs.enqueue(engine, "schedule")
                enqueued.append(engine)
        except Exception:  # noqa: BLE001 — one engine's failure must not break the rest
            _jobs.log.warning("wake_catchup.engine_failed", engine=engine, exc_info=True)
    return enqueued


# ─── WHOOP wake-trigger poller ───────────────────────────────────────────────
# A morning poller that fires the daily pipeline once WHOOP has scored the
# night's recovery — i.e. shortly after the user actually wakes. It is
# complementary to the fixed crons (daily_dag / daily_briefing): if a cron
# already produced a briefing, the wake trigger regenerates it against fresh
# data (the engine mutex serialises, so there's no conflict). The read-only
# probe lives in ``estormi_ingestion.whoop.sync``; this side owns the schedule, the
# window / once-per-morning guard, and the enqueue.

_WHOOP_POLL_JOB_ID = "whoop_polling"


async def _schedule_whoop_poll() -> None:
    """Interval-fired wake check: enqueue ingestion+briefing once per morning.

    Fires every ``whoop_polling_interval_minutes``; most ticks are a cheap
    early-return. Only when (a) polling is enabled, (b) the local hour is
    inside the configured window, (c) we haven't already fired today, and
    (d) WHOOP has scored a recovery dated today, does it enqueue the pipeline
    (ingestion then briefing — the FIFO runner serialises them) and stamp
    ``whoop_polling_last_fired_date`` so the rest of the morning is a no-op.
    """
    from estormi_server.sql.connection import _get_setting  # noqa: PLC0415

    if (await _get_setting("whoop_polling_enabled", "false")) != "true":
        return

    try:
        start_h = int(await _get_setting("whoop_polling_window_start_hour", "5"))
        end_h = int(await _get_setting("whoop_polling_window_end_hour", "11"))
    except ValueError:
        return

    now = _jobs.datetime.now()
    if now.hour < start_h:
        return  # before the morning window — nothing to do yet

    today = now.strftime("%Y-%m-%d")
    # Runtime state the poller writes back below — read from the settings table
    # only, never an env override (which would freeze it and re-fire forever or
    # never fire). See sql.connection._get_setting.
    if (await _get_setting("whoop_polling_last_fired_date", "", env_override=False)) == today:
        return

    from estormi_ingestion.shared.delivery.vault_sync import read_briefing  # noqa: PLC0415

    if now.hour >= end_h:
        # The window closed without a detected wake (e.g. no WHOOP data last
        # night). The morning cron pre-computed the briefing SILENTLY — it
        # defers delivery to this poller — so deliver it now as a fallback: the
        # user must never be left without a briefing. Stamp so it's once a day.
        existing = await asyncio.to_thread(read_briefing, today)
        if existing and existing.get("htmlBody"):
            from estormi_ingestion.shared.delivery.vault_sync import push_briefing  # noqa: PLC0415

            _jobs.log.info("whoop.wake_trigger.fallback", day=today, mode="notify-existing")
            await asyncio.to_thread(push_briefing, existing, True)  # re-push → notify
        else:
            _jobs.log.info("whoop.wake_trigger.fallback", day=today, mode="full")
            await _jobs.enqueue("ingestion", "schedule")
            await _jobs.enqueue("briefing", "schedule", payload={"notify": "force"})
        await _stamp_whoop_fired(today)
        return

    from estormi_ingestion.whoop.sync import recovery_available_today  # noqa: PLC0415

    # The probe uses sync httpx — keep the event loop free while it waits.
    recovered_day = await asyncio.to_thread(recovery_available_today)
    if recovered_day != today:
        return

    # The morning briefing usually exists by wake time (the early cron) — a
    # full re-run would take ~30 local minutes for one stale card. Refresh
    # ONLY the readiness card instead (~1 minute, see refresh_health); the
    # full pipeline stays the fallback for mornings where the cron hasn't
    # produced a briefing yet. Either path announces the briefing — this IS the
    # real wake moment (health-refresh notifies inherently; the full run is told
    # notify="force").
    existing = await asyncio.to_thread(read_briefing, today)
    if existing and existing.get("htmlBody"):
        _jobs.log.info("whoop.wake_trigger.fired", day=today, mode="health-refresh")
        await _jobs.enqueue("briefing", "schedule", payload={"refresh": "health"})
    else:
        _jobs.log.info("whoop.wake_trigger.fired", day=today, mode="full")
        await _jobs.enqueue("ingestion", "schedule")
        await _jobs.enqueue("briefing", "schedule", payload={"notify": "force"})

    await _stamp_whoop_fired(today)


async def _stamp_whoop_fired(today: str) -> None:
    """Stamp the once-per-morning guard ``whoop_polling_last_fired_date``.

    Leaf INSERT→commit span — serialised and rollback-guarded. See
    ``tools.write_txn``.
    """
    from estormi_server.storage.tools import write_txn  # noqa: PLC0415

    async with write_txn() as db:
        await db.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            ("whoop_polling_last_fired_date", today),
        )


async def apply_whoop_polling_schedule() -> None:
    """(Re)apply the WHOOP poller job from current settings.

    Adds / reschedules the interval job when polling is enabled, removes it
    when disabled. Called at startup (lifespan) and after a settings PUT so a
    toggle or interval change takes effect without a server restart — mirrors
    the daily_dag / daily_briefing reschedule dance in ``api/settings.py``.
    """
    from estormi_server.sql.connection import _get_setting  # noqa: PLC0415

    enabled = (await _get_setting("whoop_polling_enabled", "false")) == "true"
    existing = _jobs._scheduler.get_job(_WHOOP_POLL_JOB_ID)

    if not enabled:
        if existing:
            _jobs._scheduler.remove_job(_WHOOP_POLL_JOB_ID)
        return

    try:
        interval = int(await _get_setting("whoop_polling_interval_minutes", "10"))
    except ValueError:
        interval = 10
    interval = max(1, min(interval, 120))
    trigger = _jobs.IntervalTrigger(minutes=interval)

    if existing:
        _jobs._scheduler.reschedule_job(_WHOOP_POLL_JOB_ID, trigger=trigger)
    else:
        _jobs._scheduler.add_job(_schedule_whoop_poll, trigger, id=_WHOOP_POLL_JOB_ID)
