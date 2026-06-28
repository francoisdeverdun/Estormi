"""WHOOP wake-trigger poller behaviour.

``jobs._schedule_whoop_poll`` is the morning poller that fires the daily
ingestion+briefing pipeline once WHOOP has scored the night's recovery — the
proxy for "the user woke up". The guard logic (enabled / inside window / not
already fired today / recovery actually present) is the whole contract: a
break here either spams the engines every interval or never fires at all.

``jobs.apply_whoop_polling_schedule`` owns the APScheduler job lifecycle —
present when enabled, gone when disabled.

The WHOOP API, the engine queue, and SQLite are all mocked; nothing real is
spawned or hit.
"""

from __future__ import annotations

import datetime as _dt
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from estormi_server.server import jobs

pytestmark = pytest.mark.integration

# 07:00 on a fixed day — inside the default 05–11 window.
_FIXED_NOW = _dt.datetime(2026, 6, 4, 7, 0, 0)
_TODAY = "2026-06-04"


def _settings(**overrides):
    """An async ``_get_setting`` stand-in backed by a dict of defaults."""
    base = {
        "whoop_polling_enabled": "true",
        "whoop_polling_interval_minutes": "10",
        "whoop_polling_window_start_hour": "5",
        "whoop_polling_window_end_hour": "11",
        "whoop_polling_last_fired_date": "",
    }
    base.update(overrides)

    async def _get(key, default="", *, env_override=True):
        return base.get(key, default)

    return _get


def _patches(get_setting, recovery_day):
    """Patch the four collaborators the poller reaches for.

    ``sqlite_conn`` and ``enqueue`` are returned as mocks so callers can assert
    against them; ``datetime`` is pinned to ``_FIXED_NOW``.
    """
    db = MagicMock()
    db.execute = AsyncMock()
    db.commit = AsyncMock()
    enqueue = AsyncMock(return_value="queued")
    fake_dt = MagicMock(wraps=_dt.datetime)
    fake_dt.now.return_value = _FIXED_NOW
    return (
        patch("estormi_server.sql.connection._get_setting", get_setting),
        patch("estormi_server.storage.tools.sqlite_conn", return_value=db),
        patch("estormi_server.server.jobs.enqueue", enqueue),
        patch("estormi_server.server.jobs.datetime", fake_dt),
        patch("estormi_ingestion.whoop.sync.recovery_available_today", return_value=recovery_day),
        db,
        enqueue,
    )


class TestWakePoll:
    async def test_disabled_does_nothing(self):
        p1, p2, p3, p4, p5, db, enqueue = _patches(_settings(whoop_polling_enabled="false"), _TODAY)
        with p1, p2, p3, p4, p5:
            await jobs._schedule_whoop_poll()
        enqueue.assert_not_called()
        db.execute.assert_not_called()

    async def test_outside_window_does_nothing(self):
        # Window starts at 08:00 but the clock is pinned to 07:00.
        p1, p2, p3, p4, p5, db, enqueue = _patches(
            _settings(whoop_polling_window_start_hour="8"), _TODAY
        )
        with p1, p2, p3, p4, p5:
            await jobs._schedule_whoop_poll()
        enqueue.assert_not_called()

    async def test_already_fired_today_does_nothing(self):
        p1, p2, p3, p4, p5, db, enqueue = _patches(
            _settings(whoop_polling_last_fired_date=_TODAY), _TODAY
        )
        with p1, p2, p3, p4, p5:
            await jobs._schedule_whoop_poll()
        enqueue.assert_not_called()

    async def test_recovery_not_yet_scored_does_nothing(self):
        # Newest scored recovery is from yesterday — the user is not "woken" yet.
        p1, p2, p3, p4, p5, db, enqueue = _patches(_settings(), "2026-06-03")
        with p1, p2, p3, p4, p5:
            await jobs._schedule_whoop_poll()
        enqueue.assert_not_called()
        db.execute.assert_not_called()

    async def test_armed_with_recovery_fires_ingestion_then_briefing(self):
        """No briefing in the vault yet → the full pipeline path."""
        p1, p2, p3, p4, p5, db, enqueue = _patches(_settings(), _TODAY)
        with (
            p1,
            p2,
            p3,
            p4,
            p5,
            patch("estormi_ingestion.shared.delivery.vault_sync.read_briefing", return_value=None),
        ):
            await jobs._schedule_whoop_poll()
        # Order matters — the FIFO runner runs ingestion fully, then briefing.
        kinds = [c.args[0] for c in enqueue.call_args_list]
        assert kinds == ["ingestion", "briefing"]
        # The once-per-morning guard is stamped with today's local date.
        db.execute.assert_awaited_once()
        assert db.execute.await_args.args[1] == ("whoop_polling_last_fired_date", _TODAY)
        db.commit.assert_awaited_once()

    async def test_existing_briefing_gets_health_refresh_instead_of_full_run(self):
        """Briefing already composed → a ~1-minute readiness refresh, not the
        ~30-minute full pipeline."""
        p1, p2, p3, p4, p5, db, enqueue = _patches(_settings(), _TODAY)
        existing = {"date": _TODAY, "htmlBody": "<h1>Briefing</h1>"}
        with (
            p1,
            p2,
            p3,
            p4,
            p5,
            patch(
                "estormi_ingestion.shared.delivery.vault_sync.read_briefing", return_value=existing
            ),
        ):
            await jobs._schedule_whoop_poll()
        assert len(enqueue.call_args_list) == 1
        call = enqueue.call_args_list[0]
        assert call.args[0] == "briefing"
        assert call.kwargs.get("payload") == {"refresh": "health"}
        db.commit.assert_awaited_once()


class TestWakePollNotify:
    """The poller is the sole morning notifier when polling is enabled: full
    runs it triggers carry notify="force"; a window-close fallback delivers the
    silently pre-computed briefing so the user is never left without one."""

    async def test_inwindow_full_run_forces_notify(self):
        """Wake detected, no briefing yet → full run tagged notify='force' so it
        announces (the silent cron path would otherwise not)."""
        p1, p2, p3, p4, p5, db, enqueue = _patches(_settings(), _TODAY)
        with (
            p1,
            p2,
            p3,
            p4,
            p5,
            patch("estormi_ingestion.shared.delivery.vault_sync.read_briefing", return_value=None),
        ):
            await jobs._schedule_whoop_poll()
        briefing_call = enqueue.call_args_list[1]
        assert briefing_call.args[0] == "briefing"
        assert briefing_call.kwargs.get("payload") == {"notify": "force"}

    async def test_window_closed_notifies_existing_briefing(self):
        """Past the window end with no detected wake: the silently pre-computed
        briefing is delivered (re-pushed with notify) and the day is stamped."""
        noon = _dt.datetime(2026, 6, 4, 12, 0, 0)  # 12:00 ≥ end_h (11)
        db = MagicMock()
        db.execute = AsyncMock()
        db.commit = AsyncMock()
        enqueue = AsyncMock(return_value="queued")
        fake_dt = MagicMock(wraps=_dt.datetime)
        fake_dt.now.return_value = noon
        existing = {"date": _TODAY, "htmlBody": "<h1>B</h1>"}
        push = MagicMock(return_value=True)
        with (
            patch("estormi_server.sql.connection._get_setting", _settings()),
            patch("estormi_server.storage.tools.sqlite_conn", return_value=db),
            patch("estormi_server.server.jobs.enqueue", enqueue),
            patch("estormi_server.server.jobs.datetime", fake_dt),
            patch(
                "estormi_ingestion.shared.delivery.vault_sync.read_briefing", return_value=existing
            ),
            patch("estormi_ingestion.shared.delivery.vault_sync.push_briefing", push),
        ):
            await jobs._schedule_whoop_poll()
        enqueue.assert_not_called()  # already composed — just announced
        push.assert_called_once()
        assert push.call_args.args == (existing, True)  # notify=True
        db.execute.assert_awaited_once()
        assert db.execute.await_args.args[1] == ("whoop_polling_last_fired_date", _TODAY)

    async def test_window_closed_no_briefing_runs_full_with_force_notify(self):
        """Past the window end and no briefing exists → full run, notify='force'."""
        noon = _dt.datetime(2026, 6, 4, 12, 0, 0)
        db = MagicMock()
        db.execute = AsyncMock()
        db.commit = AsyncMock()
        enqueue = AsyncMock(return_value="queued")
        fake_dt = MagicMock(wraps=_dt.datetime)
        fake_dt.now.return_value = noon
        with (
            patch("estormi_server.sql.connection._get_setting", _settings()),
            patch("estormi_server.storage.tools.sqlite_conn", return_value=db),
            patch("estormi_server.server.jobs.enqueue", enqueue),
            patch("estormi_server.server.jobs.datetime", fake_dt),
            patch("estormi_ingestion.shared.delivery.vault_sync.read_briefing", return_value=None),
        ):
            await jobs._schedule_whoop_poll()
        kinds = [c.args[0] for c in enqueue.call_args_list]
        assert kinds == ["ingestion", "briefing"]
        assert enqueue.call_args_list[1].kwargs.get("payload") == {"notify": "force"}
        db.execute.assert_awaited_once()


class TestApplyPollingSchedule:
    async def test_enable_adds_job_disable_removes_it(self):
        job_id = jobs._WHOOP_POLL_JOB_ID
        # Ensure a clean slate even if a prior test leaked the job.
        if jobs._scheduler.get_job(job_id):
            jobs._scheduler.remove_job(job_id)
        try:
            with patch("estormi_server.sql.connection._get_setting", _settings()):
                await jobs.apply_whoop_polling_schedule()
            assert jobs._scheduler.get_job(job_id) is not None

            with patch(
                "estormi_server.sql.connection._get_setting",
                _settings(whoop_polling_enabled="false"),
            ):
                await jobs.apply_whoop_polling_schedule()
            assert jobs._scheduler.get_job(job_id) is None
        finally:
            if jobs._scheduler.get_job(job_id):
                jobs._scheduler.remove_job(job_id)
