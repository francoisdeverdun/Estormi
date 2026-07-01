"""Day bucketing + calendar/action formatting for the briefing.

Pure, self-contained helpers: the local-timezone resolution, UTC day bounds,
ISO parsing, and the calendar/reminder action formatting + dedupe. No
dependency on any other briefing module, so everything above (the orchestrator,
``day_context``, ``day_vision``, ``graph``) imports from here.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone

import aiosqlite

from memory_core.timeparse import parse_iso, resolve_local_tz

# resolve_local_tz lives in memory_core so the server's fetch_around anchors a
# bare-date window on the SAME local day this engine buckets into (without
# estormi_server importing estormi_briefing).
LOCAL_TZ = resolve_local_tz()


def _utc_bounds_for_local_day(day: date) -> tuple[str, str]:
    start_local = datetime.combine(day, time.min, tzinfo=LOCAL_TZ)
    end_local = datetime.combine(day, time.max, tzinfo=LOCAL_TZ)
    return (
        start_local.astimezone(timezone.utc).isoformat(),
        end_local.astimezone(timezone.utc).isoformat(),
    )


# Preserved name: used by day_vision, day_context, and graph. The
# implementation now lives in memory_core.
_parse_iso_datetime = parse_iso


def _is_all_day_raw(raw: str | None) -> bool | None:
    """Decide all-day from the *raw* source date string, when it's authoritative.

    A connector preserves Google's (and Apple's) all-day signal in *which form*
    it stores the start: an all-day event is a bare ``YYYY-MM-DD`` (no ``T``, no
    offset), a timed one a full ISO datetime. That distinction is the only
    reliable discriminator — a real 00:00 commitment is still a ``dateTime`` and
    must keep its time. Returns:

      * ``True``  — a bare date → all-day;
      * ``False`` — a datetime with a time component → timed;
      * ``None``  — no usable signal (empty / unparseable), so the caller falls
        back to the legacy midnight heuristic for chunks ingested before the
        ``date`` field carried this distinction faithfully.
    """
    s = (raw or "").strip()
    if not s:
        return None
    # A bare calendar date is exactly 10 chars (YYYY-MM-DD) with no time marker.
    if "T" not in s and ":" not in s:
        try:
            datetime.strptime(s[:10], "%Y-%m-%d")
        except ValueError:
            return None
        return True
    return False


def _is_overdue(date_ts_str: str, after_utc: str) -> bool:
    """True when ``date_ts_str`` falls strictly before the day-start ``after_utc``.

    Compare real instants, never raw ISO text. Reminders store their due time as
    ``…Z`` (export_reminders ``_format_due``) while ``after_utc`` is emitted as
    ``…+00:00`` (``_utc_bounds_for_local_day``); ``Z`` (0x5A) sorts *after* ``+``
    (0x2B), so a lexical ``<`` would call a same-or-earlier instant "not overdue"
    purely because of the offset spelling. Parsing both to aware datetimes makes
    the comparison offset-agnostic.
    """
    if not (date_ts_str and after_utc):
        return False
    due = _parse_iso_datetime(date_ts_str)
    start = _parse_iso_datetime(after_utc)
    if due is None or start is None:
        return False
    return due < start


def _format_action(row: aiosqlite.Row, after_utc: str = "") -> dict:
    dt = _parse_iso_datetime(row["date_ts"] or row["date"])
    source = row["source"] or ""
    # Calendar rows arrive under either `calendar` or `gcal`; only `reminders`
    # is the reminder source, so label by exclusion rather than enumerating.
    source_label = "Reminders" if source == "reminders" else "Calendar"

    when = ""
    if dt:
        # Primary signal: the raw source date. A bare YYYY-MM-DD is an authoritative
        # all-day marker; a datetime is timed (even at local/UTC midnight). Only
        # when the raw form gives no signal (older chunks) do we fall back to the
        # legacy midnight heuristic, which mislabels genuine 00:00 commitments.
        all_day = _is_all_day_raw(row["date"])
        if all_day is None:
            dt_utc = dt.astimezone(timezone.utc)
            dt_local = dt.astimezone(LOCAL_TZ)
            all_day = (dt_utc.hour == 0 and dt_utc.minute == 0 and dt_utc.second == 0) or (
                dt_local.hour == 0 and dt_local.minute == 0 and dt_local.second == 0
            )
        when = "All day" if all_day else dt.astimezone(LOCAL_TZ).strftime("%H:%M")

    date_ts_str = row["date_ts"] or ""
    # `event_status` / `event_type` are calendar-only columns; the reminders
    # query omits them, so read defensively (the row is an aiosqlite.Row).
    keys = row.keys()
    event_status = row["event_status"] if "event_status" in keys else None
    event_type = (row["event_type"] if "event_type" in keys else None) or "default"
    working_location = (row["working_location"] if "working_location" in keys else "") or ""
    return {
        "source": source_label,
        "when": when,
        "title": row["title"] or "(untitled)",
        "group_type": row["group_type"] or "unknown",
        "context_id": row["chat_id_raw"] or "",
        "date_ts": date_ts_str,
        # A "maybe" RSVP (gcal status=tentative) makes the slot uncertain; the
        # event type tells a real meeting from an absence (outOfOffice) or a
        # blocked focus slot (focusTime). Both ride on the SQLite row now, so
        # the day-vision sees them directly without a text round-trip.
        "tentative": event_status == "tentative",
        "event_type": event_type,
        # Google working-location label for the day ("home office", an office
        # site code …) — lets the day-vision place the user (home vs an office
        # site) and avoid suggesting a commute on a remote day.
        "working_location": working_location,
        "overdue": _is_overdue(date_ts_str, after_utc),
        # Whole days past due (0 when not overdue) — the overdue list orders by
        # this (most-recently-overdue first) and shows a "· depuis N j" age
        # affordance. Deterministic (anchored on the day-start, not now()).
        "days_overdue": _days_overdue(date_ts_str, after_utc),
        # A *timed* reminder whose slot passed more than a day before this
        # briefing began is stale, not a live errand: a 14:00 "call the plumber"
        # from three days ago is gone, while a date-only chore ("renew passport")
        # still rolls forward until done. build_daily_note drops expired items
        # from the overdue list + count so a chronic timed backlog stops padding
        # every morning. Anchored on the briefing day-start (after_utc), so it's
        # deterministic — no wall-clock read.
        "expired": _is_expired(date_ts_str, row["date"], after_utc),
    }


# Grace after a timed reminder's slot before it counts as stale (expired), not a
# live errand. A day covers "I meant to do it last night" without letting a chore
# from last week keep padding the overdue list every morning.
_EXPIRED_GRACE = timedelta(hours=24)


def _is_expired(date_ts_str: str, raw_date: str | None, after_utc: str) -> bool:
    """True when a *timed* reminder aged past the grace window before day-start.

    Only timed reminders expire: an all-day / date-only chore (bare
    ``YYYY-MM-DD``) keeps rolling forward until it's done. Compares real instants
    against the tz-aware briefing day-start, never raw ISO text (same offset
    caveat as ``_is_overdue``)."""
    if _is_all_day_raw(raw_date) is not False:
        return False  # date-only or no signal → never expires, keeps rolling
    due = _parse_iso_datetime(date_ts_str)
    start = _parse_iso_datetime(after_utc)
    if due is None or start is None:
        return False
    return (start - due) > _EXPIRED_GRACE


def _days_overdue(date_ts_str: str, after_utc: str) -> int:
    """Whole days between a reminder's due instant and the briefing day-start.

    The age affordance the overdue list shows ("· depuis N j"): how long a live
    reminder has been sitting past due. Anchored on the tz-aware day-start
    (``after_utc``), never wall-clock, so a past-day rebuild is deterministic;
    compares real instants, never raw ISO text (same offset caveat as
    ``_is_overdue``). Floors at 0 — a not-yet-overdue or unparseable due date
    yields 0, so the caller can gate the affordance on ``> 0``."""
    due = _parse_iso_datetime(date_ts_str)
    start = _parse_iso_datetime(after_utc)
    if due is None or start is None:
        return 0
    delta_days = (start - due).days
    return delta_days if delta_days > 0 else 0


_WEEKDAYS_EN = (
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
    "Friday",
    "Saturday",
    "Sunday",
)


def _local_when_label(date_ts: str | None, raw_date: str | None = None) -> str:
    """Normalised local-time label for a stored (UTC) timestamp.

    Returns e.g. ``2026-06-02 (Tuesday), all day`` or ``2026-05-31 (Sunday) 19:30``.

    The day-vision context carries raw UTC timestamps *inside* item text — a
    reminder's ``Due: …Z`` line — and the LLM used to read those literally, so a
    local-midnight item (stored as 22:00Z the previous day) was mis-dated by a
    day and announced at the wrong time ("demain soir" for what is really
    Tuesday, all day). Handing the model the already-localised date removes the
    timezone arithmetic it kept getting wrong.

    ``raw_date`` is the unnormalised source date string (the chunk ``date``
    field). When present it is the authoritative all-day signal — a bare
    ``YYYY-MM-DD`` means all-day, a datetime means timed — so a genuine
    midnight-ish commitment keeps its time. Absent or unparseable, we fall back
    to the legacy midnight heuristic for older data.
    """
    dt = _parse_iso_datetime(date_ts)
    if not dt:
        return ""
    loc = dt.astimezone(LOCAL_TZ)
    all_day = _is_all_day_raw(raw_date)
    if all_day is None:
        utc = dt.astimezone(timezone.utc)
        all_day = (loc.hour == 0 and loc.minute == 0) or (
            utc.hour == 0 and utc.minute == 0 and utc.second == 0
        )
    label = f"{loc.date().isoformat()} ({_WEEKDAYS_EN[loc.weekday()]})"
    return f"{label}, all day" if all_day else f"{label} {loc.strftime('%H:%M')}"


def _day_anchor(day: date) -> str:
    """Explicit today/tomorrow/day-after anchor so the vision never guesses days."""

    def lbl(d: date) -> str:
        return f"{d.isoformat()} ({_WEEKDAYS_EN[d.weekday()]})"

    return (
        f"Today is {lbl(day)}. Tomorrow is {lbl(day + timedelta(days=1))}; "
        f"the day after is {lbl(day + timedelta(days=2))}."
    )


def _action_key(action: dict) -> str:
    return " ".join(str(action.get("title") or "").lower().split())


def _dedupe_calendar_reminders(
    calendar: list[dict], reminders: list[dict]
) -> tuple[list[dict], list[dict]]:
    """Collapse Calendar/Reminder duplicates while preserving the more credible time."""
    reminders_by_title = {_action_key(r): r for r in reminders if _action_key(r)}
    calendar_out: list[dict] = []
    consumed_reminders: set[str] = set()

    for event in calendar:
        key = _action_key(event)
        reminder = reminders_by_title.get(key)
        if not reminder:
            calendar_out.append(event)
            continue

        consumed_reminders.add(key)
        if reminder.get("when") and reminder.get("when") != event.get("when"):
            calendar_out.append(
                {**event, "when": reminder["when"], "source": "Calendar + Reminders"}
            )
        else:
            calendar_out.append(event)

    reminders_out = [r for r in reminders if _action_key(r) not in consumed_reminders]
    return calendar_out, reminders_out
