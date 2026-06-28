"""Settings-update validation — the business rules behind ``PUT /api/settings``.

The route handler (:func:`estormi_server.api.settings.put_settings`) owns the
HTTP shell: it parses the body, calls :func:`validate_settings_update`, maps a
returned :class:`SettingsValidationError` to the right status code, then does
the upsert + scheduler side effects. Every guard here is a pure function of the
``updates`` dict, so the whole validation matrix (caps, protected keys, cron
syntax, bounded WHOOP knobs, language / TTS-voice enums) is unit-testable
without an ASGI client.
"""

from __future__ import annotations

from dataclasses import dataclass

from apscheduler.triggers.cron import CronTrigger

# Keys that are credentials or server-managed internal state. The generic
# ``PUT /api/settings`` upsert must not be a back door for writing them — they
# are minted/rotated/owned by their own dedicated code paths (pairing QR, OAuth
# callback, knowledge-sources path, gcal sync/selection, schedulers, lifespan).
# Every server-managed key MUST be protected: list the fixed ones here and the
# dynamic families (permission cache) in :func:`_is_protected_key`.
PROTECTED_SETTING_KEYS = frozenset(
    {
        "pairing_token",
        "pairing_token_issued_at",
        "knowledge_sources_yaml",
        # Written only by the Google-Calendar sync (the incremental syncToken
        # cursor); a generic PUT must not corrupt it into a forced full resync.
        "google_calendar_sync_token",
        # Calendar selection + life-context tagging owned by the /api/calendar
        # endpoints (stored as JSON); a generic PUT must not override them.
        "google_calendar_selected_ids",
        "google_calendar_group_types",
        # Scheduler-owned WHOOP wake-poller day cursor; a generic PUT could
        # suppress the poller for the day or force a re-fire.
        "whoop_polling_last_fired_date",
        # One-time migration flag (lifespan); flipping it re-runs/suppresses the
        # chat-kind backfill.
        "chat_kind_backfilled",
        # Briefing-engine run status, written by the knowledge service.
        "knowledge_last_run_status",
        # Server-managed embedding-model identity (set at lifespan startup); a
        # generic PUT could induce an embedding-dimension mismatch on next ingest.
        "embed_model",
    }
)


# Dynamic server-managed key FAMILIES the exact-match set above can't enumerate:
# the preflight permission cache writes per-source ``source_<name>_permission``
# and per-volume ``volume_permission_<id>`` keys. Guarded by prefix/suffix in
# :func:`_is_protected_key`.
def _is_protected_key(key: str) -> bool:
    if key in PROTECTED_SETTING_KEYS:
        return True
    return (key.startswith("source_") and key.endswith("_permission")) or key.startswith(
        "volume_permission_"
    )


# Hard ceiling on a single settings value. The settings table is for short
# scalar config (cron strings, depth pickers, JSON IDs); a 4 KB cap is enough
# for any legitimate use and stops a runaway caller from dumping multi-MB blobs
# into a single row.
MAX_SETTING_VALUE_LEN = 4096

# Hard ceiling on the number of keys in a single PUT. The settings table has
# ~50 known keys today; 500 is generous headroom that still rejects a caller
# trying to flood the table with junk keys in one shot.
MAX_SETTING_KEYS_PER_PUT = 500

# The cron keys in use: the daily ingestion pipeline, the daily briefing, and
# the weekly quill distillation. "manual" is the documented opt-out and bypasses
# the parser.
_CRON_KEYS = ("schedule_cron", "briefing_schedule_cron", "distill_schedule_cron")

# WHOOP wake-trigger poller knobs: (key, lo, hi) inclusive bounds.
_WHOOP_BOUNDED_INTS = (
    ("whoop_polling_interval_minutes", 1, 120),
    ("whoop_polling_window_start_hour", 0, 23),
    ("whoop_polling_window_end_hour", 0, 23),
)


@dataclass(frozen=True)
class SettingsValidationError:
    """A rejected settings update: ``message`` is the client-facing reason and
    ``status_code`` the HTTP status the route should return."""

    message: str
    status_code: int


def validate_settings_update(updates: dict[str, str]) -> SettingsValidationError | None:
    """Validate a ``PUT /api/settings`` body. Return ``None`` when the update is
    acceptable, or a :class:`SettingsValidationError` describing the first
    violation found (and the status code the route should reply with).

    Guards, in order: key-count cap, protected keys, value-length cap, cron
    syntax, WHOOP poller bounds, briefing language enum, briefing TTS voice.
    """
    if len(updates) > MAX_SETTING_KEYS_PER_PUT:
        return SettingsValidationError(f"too many keys (max {MAX_SETTING_KEYS_PER_PUT})", 422)

    rejected = sorted(k for k in updates if _is_protected_key(k))
    if rejected:
        return SettingsValidationError(
            f"protected settings keys cannot be written here: {rejected}", 400
        )

    too_long = sorted(
        k for k, v in updates.items() if isinstance(v, str) and len(v) > MAX_SETTING_VALUE_LEN
    )
    if too_long:
        return SettingsValidationError(
            f"settings values exceed {MAX_SETTING_VALUE_LEN}-char cap: {too_long}", 400
        )

    # Reject malformed crontabs up front so a typo doesn't blow up the
    # scheduler asynchronously after the write.
    for cron_key in _CRON_KEYS:
        if cron_key in updates and updates[cron_key] != "manual":
            try:
                CronTrigger.from_crontab(updates[cron_key])
            except (ValueError, TypeError) as exc:
                return SettingsValidationError(f"invalid {cron_key}: {exc}", 400)

    for key, lo, hi in _WHOOP_BOUNDED_INTS:
        err = _bounded_int(updates, key, lo, hi)
        if err is not None:
            return err
    if "whoop_polling_window_start_hour" in updates and "whoop_polling_window_end_hour" in updates:
        if int(updates["whoop_polling_window_start_hour"]) >= int(
            updates["whoop_polling_window_end_hour"]
        ):
            return SettingsValidationError(
                "whoop_polling_window_start_hour must be < end_hour", 400
            )

    # briefing_language drives the LLM "write in X" directive in the briefing
    # engine. The Florilegium is French-only since the English switch was
    # retired (the composer emits French unconditionally), so 'fr' is the sole
    # valid value; the latent 'en' chrome is kept for a possible future
    # bilingual edition but is not user-selectable.
    if "briefing_language" in updates and updates["briefing_language"] != "fr":
        return SettingsValidationError("invalid briefing_language (expected 'fr')", 400)

    # briefing_tts_voice must be one of Voxtral's preset narrators (validated
    # against the engine's own list so the two never drift). Empty resets to
    # the automatic choice — the voice matching the briefing language (see
    # estormi_briefing/io/delivery.py).
    if "briefing_tts_voice" in updates and updates["briefing_tts_voice"] != "":
        from memory_core.tts_local import VALID_VOICES  # noqa: PLC0415

        if updates["briefing_tts_voice"] not in VALID_VOICES:
            return SettingsValidationError("invalid briefing_tts_voice", 400)

    # briefing_tts_model must name a TTS catalog entry (empty resets to the
    # default model). The catalog has one model today; the picker persists the
    # choice so a second model needs no settings change.
    if "briefing_tts_model" in updates and updates["briefing_tts_model"] != "":
        from memory_core.tts_local import TTS_CATALOG  # noqa: PLC0415

        if updates["briefing_tts_model"] not in TTS_CATALOG:
            return SettingsValidationError("invalid briefing_tts_model", 400)

    return None


def _bounded_int(
    updates: dict[str, str], key: str, lo: int, hi: int
) -> SettingsValidationError | None:
    """Validate an optional integer settings value lies within ``[lo, hi]``."""
    if key not in updates:
        return None
    try:
        n = int(updates[key])
    except (ValueError, TypeError):
        return SettingsValidationError(f"invalid {key}: expected an integer", 400)
    if not (lo <= n <= hi):
        return SettingsValidationError(f"invalid {key}: expected {lo}–{hi}", 400)
    return None
