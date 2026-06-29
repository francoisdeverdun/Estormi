"""Tests for ``/api/settings`` validation **and** PUT scheduler side-effects.

Two distinct layers live here, each with its own marker:

* ``unit`` — the pure ``validate_settings_update`` matrix (caps, protected
  keys, cron syntax, WHOOP poller bounds, language / TTS-voice enums),
  exercised directly with no HTTP layer in the way.
* ``integration`` — the ``PUT /api/settings`` handler's *side effects*: a
  changed briefing / distill cron must (re)schedule the matching APScheduler
  job with the right trigger and callable, and any ``whoop_polling_*`` change
  must re-apply the WHOOP poller. The scheduler boundary is mocked so we can
  assert on the exact calls and their arguments — behaviour, not just that a
  function ran. (The ``daily_dag`` ingestion cron is covered against the *real*
  scheduler in ``test_main_full.py``; the briefing / distill / WHOOP seams are
  covered here.)
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from estormi_server.services.settings import (
    MAX_SETTING_KEYS_PER_PUT,
    MAX_SETTING_VALUE_LEN,
    SettingsValidationError,
    validate_settings_update,
)


@pytest.mark.unit
class TestAccepts:
    def test_empty_update_is_valid(self):
        assert validate_settings_update({}) is None

    def test_plain_scalar_keys_are_valid(self):
        assert validate_settings_update({"foo": "bar", "depth": "90"}) is None

    def test_manual_cron_bypasses_parser(self):
        assert validate_settings_update({"schedule_cron": "manual"}) is None
        assert validate_settings_update({"briefing_schedule_cron": "manual"}) is None

    def test_valid_crontab_accepted(self):
        assert validate_settings_update({"schedule_cron": "0 2 * * *"}) is None

    def test_value_at_cap_is_valid(self):
        assert validate_settings_update({"k": "x" * MAX_SETTING_VALUE_LEN}) is None

    def test_valid_whoop_window(self):
        assert (
            validate_settings_update(
                {
                    "whoop_polling_interval_minutes": "15",
                    "whoop_polling_window_start_hour": "6",
                    "whoop_polling_window_end_hour": "22",
                }
            )
            is None
        )

    def test_valid_language(self):
        assert validate_settings_update({"briefing_language": "fr"}) is None

    def test_english_language_rejected(self):
        # English was retired with the French-only edition; the composer emits
        # French unconditionally, so 'en' is no longer an accepted setting.
        err = validate_settings_update({"briefing_language": "en"})
        assert isinstance(err, SettingsValidationError)
        assert err.status_code == 400


@pytest.mark.unit
class TestKeyAndValueCaps:
    def test_too_many_keys_returns_422(self):
        updates = {f"k{i}": "v" for i in range(MAX_SETTING_KEYS_PER_PUT + 1)}
        err = validate_settings_update(updates)
        assert isinstance(err, SettingsValidationError)
        assert err.status_code == 422
        assert "too many keys" in err.message

    def test_value_over_cap_returns_400(self):
        err = validate_settings_update({"k": "x" * (MAX_SETTING_VALUE_LEN + 1)})
        assert isinstance(err, SettingsValidationError)
        assert err.status_code == 400
        assert "exceed" in err.message
        assert "k" in err.message


@pytest.mark.unit
class TestProtectedKeys:
    @pytest.mark.parametrize(
        "key",
        [
            "pairing_token",
            "pairing_token_issued_at",
            "knowledge_sources_yaml",
            # Server-managed state added so the generic PUT can't corrupt it.
            "google_calendar_sync_token",
            "google_calendar_selected_ids",
            "google_calendar_group_types",
            "whoop_polling_last_fired_date",
            "chat_kind_backfilled",
            "knowledge_last_run_status",
            "embed_model",
            # Dynamic permission-cache families (matched by prefix/suffix).
            "source_imessage_permission",
            "volume_permission_disk1",
        ],
    )
    def test_protected_key_rejected_400(self, key):
        err = validate_settings_update({key: "x"})
        assert isinstance(err, SettingsValidationError)
        assert err.status_code == 400
        assert "protected" in err.message

    def test_protected_takes_priority_over_other_valid_keys(self):
        err = validate_settings_update({"foo": "bar", "pairing_token": "x"})
        assert err is not None and err.status_code == 400

    def test_ordinary_user_key_still_allowed(self):
        # A normal user-writable config key must NOT trip the prefix matcher.
        assert validate_settings_update({"briefing_home_location": "Paris, France"}) is None


@pytest.mark.unit
class TestCron:
    def test_invalid_crontab_rejected_400(self):
        err = validate_settings_update({"schedule_cron": "not a cron"})
        assert isinstance(err, SettingsValidationError)
        assert err.status_code == 400
        assert "invalid schedule_cron" in err.message

    def test_invalid_briefing_cron_rejected_400(self):
        err = validate_settings_update({"briefing_schedule_cron": "99 99 99 99 99"})
        assert err is not None and err.status_code == 400
        assert "briefing_schedule_cron" in err.message

    def test_distill_cron_validated(self):
        # The weekly quill-retrain cron is a first-class schedule key.
        assert validate_settings_update({"distill_schedule_cron": "0 4 * * 0"}) is None
        assert validate_settings_update({"distill_schedule_cron": "manual"}) is None
        err = validate_settings_update({"distill_schedule_cron": "not a cron"})
        assert err is not None and err.status_code == 400
        assert "distill_schedule_cron" in err.message


@pytest.mark.unit
class TestWhoopBounds:
    def test_non_integer_interval_rejected(self):
        err = validate_settings_update({"whoop_polling_interval_minutes": "soon"})
        assert err is not None and err.status_code == 400
        assert "expected an integer" in err.message

    def test_interval_out_of_range_rejected(self):
        err = validate_settings_update({"whoop_polling_interval_minutes": "0"})
        assert err is not None and err.status_code == 400
        assert "expected 1–120" in err.message

    def test_hour_out_of_range_rejected(self):
        err = validate_settings_update({"whoop_polling_window_start_hour": "24"})
        assert err is not None and err.status_code == 400

    def test_start_must_be_before_end(self):
        err = validate_settings_update(
            {
                "whoop_polling_window_start_hour": "22",
                "whoop_polling_window_end_hour": "6",
            }
        )
        assert err is not None and err.status_code == 400
        assert "start_hour must be < end_hour" in err.message

    def test_equal_start_end_rejected(self):
        err = validate_settings_update(
            {
                "whoop_polling_window_start_hour": "8",
                "whoop_polling_window_end_hour": "8",
            }
        )
        assert err is not None and err.status_code == 400


@pytest.mark.unit
class TestEnums:
    def test_invalid_language_rejected(self):
        err = validate_settings_update({"briefing_language": "de"})
        assert err is not None and err.status_code == 400
        assert "briefing_language" in err.message

    def test_valid_tts_voice_accepted(self):
        from memory_core.tts_local import VALID_VOICES

        voice = next(iter(VALID_VOICES))
        assert validate_settings_update({"briefing_tts_voice": voice}) is None

    def test_invalid_tts_voice_rejected(self):
        err = validate_settings_update({"briefing_tts_voice": "robot9000"})
        assert err is not None and err.status_code == 400
        assert "briefing_tts_voice" in err.message

    def test_empty_tts_voice_accepted_as_auto(self):
        # "" resets the selector to automatic (voice matching the briefing
        # language, resolved in estormi_briefing/io/delivery.py).
        assert validate_settings_update({"briefing_tts_voice": ""}) is None

    def test_valid_tts_model_accepted(self):
        from memory_core.tts_local import TTS_CATALOG

        model = next(iter(TTS_CATALOG))
        assert validate_settings_update({"briefing_tts_model": model}) is None
        assert validate_settings_update({"briefing_tts_model": ""}) is None

    def test_invalid_tts_model_rejected(self):
        err = validate_settings_update({"briefing_tts_model": "gpt-tts-9000"})
        assert err is not None and err.status_code == 400
        assert "briefing_tts_model" in err.message


# ── PUT /api/settings scheduler side-effects ───────────────────────────────────


def _cron_hour(trigger) -> str:
    """Pull the hour field out of an APScheduler ``CronTrigger`` repr.

    The handler builds the trigger via the *real* ``CronTrigger.from_crontab``,
    so asserting on the hour proves the requested cron value actually reached
    the scheduler — not merely that *some* trigger object was passed through.
    """
    return repr(trigger)


@pytest.fixture
def mock_scheduler():
    """Replace the module-level APScheduler the PUT handler reaches for.

    Patching ``api.settings._scheduler`` (the name bound at import) lets us
    observe the exact ``add_job`` / ``reschedule_job`` / ``remove_job`` calls
    without touching the process-wide real scheduler that other tests and the
    live server share. ``get_job`` is parameterised per-test to model the
    "job already exists" vs "first registration" branches.
    """
    sched = MagicMock(name="scheduler")
    sched.get_job.return_value = None  # default: no job registered yet
    with patch("estormi_server.api.settings._scheduler", sched):
        yield sched


@pytest.fixture
def mock_whoop_apply():
    """Stub the WHOOP poller re-apply hook so we can assert it is awaited."""
    hook = AsyncMock(name="apply_whoop_polling_schedule")
    with patch("estormi_server.api.settings.apply_whoop_polling_schedule", hook):
        yield hook


@pytest.mark.integration
class TestPutBriefingSchedule:
    """A changed ``briefing_schedule_cron`` must drive the ``daily_briefing`` job."""

    async def test_first_cron_registers_briefing_job(self, client, mock_scheduler):
        from estormi_server.api import settings as settings_mod

        resp = await client.put("/api/settings", json={"briefing_schedule_cron": "30 7 * * *"})
        assert resp.status_code == 200
        assert resp.json()["briefing_schedule_cron"] == "30 7 * * *"

        # No prior job → add_job, never reschedule.
        mock_scheduler.add_job.assert_called_once()
        mock_scheduler.reschedule_job.assert_not_called()
        args, kwargs = mock_scheduler.add_job.call_args
        # Behavioural assertions: the wrapped briefing callable, the right id,
        # and a trigger that actually encodes the requested 07:30 cron.
        assert args[0] is settings_mod._schedule_briefing
        assert kwargs["id"] == "daily_briefing"
        trigger = args[1]
        assert "hour='7'" in _cron_hour(trigger)
        assert "minute='30'" in _cron_hour(trigger)

    async def test_existing_job_is_rescheduled_not_duplicated(self, client, mock_scheduler):
        # Model an already-registered job: get_job returns a truthy handle.
        mock_scheduler.get_job.return_value = MagicMock(name="existing_briefing_job")

        resp = await client.put("/api/settings", json={"briefing_schedule_cron": "0 9 * * *"})
        assert resp.status_code == 200

        mock_scheduler.reschedule_job.assert_called_once()
        mock_scheduler.add_job.assert_not_called()
        args, kwargs = mock_scheduler.reschedule_job.call_args
        assert args[0] == "daily_briefing"
        assert "hour='9'" in _cron_hour(kwargs["trigger"])

    async def test_manual_removes_existing_briefing_job(self, client, mock_scheduler):
        mock_scheduler.get_job.return_value = MagicMock(name="existing_briefing_job")

        resp = await client.put("/api/settings", json={"briefing_schedule_cron": "manual"})
        assert resp.status_code == 200

        mock_scheduler.remove_job.assert_called_once_with("daily_briefing")
        mock_scheduler.add_job.assert_not_called()
        mock_scheduler.reschedule_job.assert_not_called()

    async def test_manual_with_no_job_is_a_noop(self, client, mock_scheduler):
        # get_job → None: nothing to remove, and nothing is added either.
        resp = await client.put("/api/settings", json={"briefing_schedule_cron": "manual"})
        assert resp.status_code == 200
        mock_scheduler.remove_job.assert_not_called()
        mock_scheduler.add_job.assert_not_called()

    async def test_unrelated_put_leaves_briefing_scheduler_untouched(self, client, mock_scheduler):
        # A PUT that changes no schedule key must not touch the scheduler.
        resp = await client.put("/api/settings", json={"briefing_home_location": "Paris"})
        assert resp.status_code == 200
        mock_scheduler.add_job.assert_not_called()
        mock_scheduler.reschedule_job.assert_not_called()
        mock_scheduler.remove_job.assert_not_called()


@pytest.mark.integration
class TestPutDistillSchedule:
    """A changed ``distill_schedule_cron`` must drive the ``weekly_distill`` job."""

    async def test_first_cron_registers_distill_job(self, client, mock_scheduler):
        from estormi_server.api import settings as settings_mod

        # Sunday 04:00 weekly retrain.
        resp = await client.put("/api/settings", json={"distill_schedule_cron": "0 4 * * 0"})
        assert resp.status_code == 200

        mock_scheduler.add_job.assert_called_once()
        args, kwargs = mock_scheduler.add_job.call_args
        assert args[0] is settings_mod._schedule_distill
        assert kwargs["id"] == "weekly_distill"
        assert "hour='4'" in _cron_hour(args[1])

    async def test_existing_distill_job_rescheduled(self, client, mock_scheduler):
        mock_scheduler.get_job.return_value = MagicMock(name="existing_distill_job")

        resp = await client.put("/api/settings", json={"distill_schedule_cron": "0 5 * * 1"})
        assert resp.status_code == 200

        mock_scheduler.reschedule_job.assert_called_once()
        args, kwargs = mock_scheduler.reschedule_job.call_args
        assert args[0] == "weekly_distill"
        assert "hour='5'" in _cron_hour(kwargs["trigger"])

    async def test_manual_removes_existing_distill_job(self, client, mock_scheduler):
        mock_scheduler.get_job.return_value = MagicMock(name="existing_distill_job")

        resp = await client.put("/api/settings", json={"distill_schedule_cron": "manual"})
        assert resp.status_code == 200
        mock_scheduler.remove_job.assert_called_once_with("weekly_distill")


@pytest.mark.integration
class TestPutWhoopReapply:
    """Any ``whoop_polling_*`` change must re-apply the WHOOP poller schedule."""

    async def test_interval_change_reapplies_whoop(self, client, mock_whoop_apply):
        resp = await client.put("/api/settings", json={"whoop_polling_interval_minutes": "15"})
        assert resp.status_code == 200
        mock_whoop_apply.assert_awaited_once_with()

    async def test_window_change_reapplies_whoop(self, client, mock_whoop_apply):
        resp = await client.put(
            "/api/settings",
            json={
                "whoop_polling_window_start_hour": "6",
                "whoop_polling_window_end_hour": "22",
            },
        )
        assert resp.status_code == 200
        # A single PUT re-applies exactly once regardless of how many
        # whoop_polling_* knobs it touches.
        mock_whoop_apply.assert_awaited_once_with()

    async def test_non_whoop_put_does_not_reapply(self, client, mock_whoop_apply):
        resp = await client.put("/api/settings", json={"briefing_home_location": "Lyon"})
        assert resp.status_code == 200
        mock_whoop_apply.assert_not_awaited()

    async def test_rejected_whoop_put_never_reaches_reapply(self, client, mock_whoop_apply):
        # An out-of-range interval fails validation up front (422/400), so the
        # poller hook must never fire — the side effect is gated behind a
        # successful upsert.
        resp = await client.put("/api/settings", json={"whoop_polling_interval_minutes": "0"})
        assert resp.status_code == 400
        mock_whoop_apply.assert_not_awaited()


@pytest.mark.integration
class TestPutSchedulerIsolation:
    """A rejected PUT performs no scheduler side effects at all."""

    async def test_rejected_cron_does_not_touch_scheduler(self, client, mock_scheduler):
        resp = await client.put("/api/settings", json={"briefing_schedule_cron": "not a cron"})
        assert resp.status_code == 400
        mock_scheduler.add_job.assert_not_called()
        mock_scheduler.reschedule_job.assert_not_called()
        mock_scheduler.remove_job.assert_not_called()
