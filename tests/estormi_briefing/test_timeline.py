"""timeline — the code-rendered schedule strip (free slots + HTML)."""

from __future__ import annotations

from datetime import date, datetime

import pytest

from estormi_briefing.compose.timeline import _fmt_minutes, free_slots, timeline_html
from estormi_briefing.day.day import LOCAL_TZ

pytestmark = pytest.mark.unit

DAY = date(2026, 6, 12)
LABELS = {"free_slot": "Free", "all_day": "All day"}


def _dt(hour: int, minute: int = 0) -> datetime:
    return datetime(DAY.year, DAY.month, DAY.day, hour, minute, tzinfo=LOCAL_TZ)


def _ev(title: str, start: datetime | None, end: datetime | None) -> dict:
    return {"title": title, "start": start, "end": end}


# ── free_slots ────────────────────────────────────────────────────────────────


def test_free_slots_empty_day_is_one_full_window_slot():
    slots = free_slots([], DAY)
    assert slots == [{"start": _dt(8), "end": _dt(21), "minutes": 13 * 60}]


def test_free_slots_merges_nested_and_back_to_back_events():
    """An event inside another, or touching the next, must not open a phantom
    slot between them: busy is 09:00–12:00, full stop."""
    events = [
        _ev("Long block", _dt(9), _dt(11)),
        _ev("Nested", _dt(10), _dt(10, 30)),
        _ev("Back-to-back", _dt(11), _dt(12)),
    ]
    slots = free_slots(events, DAY)
    assert [(s["start"], s["end"]) for s in slots] == [
        (_dt(8), _dt(9)),
        (_dt(12), _dt(21)),
    ]


def test_free_slots_clamps_events_straddling_the_window():
    """A 07:30–09:00 meeting eats the 08:00–09:00 part; a 20:00–22:00 one
    eats the tail."""
    events = [
        _ev("Early", _dt(7, 30), _dt(9)),
        _ev("Late", _dt(20), datetime(2026, 6, 12, 22, tzinfo=LOCAL_TZ)),
    ]
    slots = free_slots(events, DAY)
    assert [(s["start"], s["end"]) for s in slots] == [(_dt(9), _dt(20))]
    assert slots[0]["minutes"] == 11 * 60


def test_free_slots_drops_gaps_under_min_minutes():
    events = [_ev("Morning", _dt(9), _dt(10)), _ev("Rest", _dt(10, 30), _dt(21))]
    slots = free_slots(events, DAY)
    # The 30-min gap at 10:00 is below the 45-min floor; only 08:00–09:00 stays.
    assert [(s["start"], s["end"], s["minutes"]) for s in slots] == [(_dt(8), _dt(9), 60)]


def test_free_slots_min_minutes_override():
    events = [_ev("Morning", _dt(9), _dt(10)), _ev("Rest", _dt(10, 30), _dt(21))]
    slots = free_slots(events, DAY, min_minutes=30)
    assert [(s["start"], s["end"]) for s in slots] == [
        (_dt(8), _dt(9)),
        (_dt(10), _dt(10, 30)),
    ]


def test_free_slots_point_event_occupies_nothing():
    """end <= start is a point (a reminder-ish marker): the window stays whole."""
    events = [_ev("Ping", _dt(10), _dt(10)), _ev("Inverted", _dt(15), _dt(14))]
    slots = free_slots(events, DAY)
    assert slots == [{"start": _dt(8), "end": _dt(21), "minutes": 13 * 60}]


def test_free_slots_fully_busy_day_has_no_slots():
    assert free_slots([_ev("Marathon", _dt(7), _dt(22))], DAY) == []


def test_free_slots_honors_custom_window():
    slots = free_slots([], DAY, start_hour=9, end_hour=10)
    assert slots == [{"start": _dt(9), "end": _dt(10), "minutes": 60}]


# ── timeline_html ─────────────────────────────────────────────────────────────


def test_timeline_html_empty_events_returns_empty():
    assert timeline_html([], [{"start": _dt(8), "end": _dt(21), "minutes": 780}], LABELS) == ""


def test_timeline_html_renders_events_and_interleaves_slots():
    events = [_ev("Standup", _dt(9, 45), _dt(10, 30)), _ev("Review", _dt(14), _dt(15))]
    slots = free_slots(events, DAY)
    html = timeline_html(events, slots, LABELS)

    assert html.startswith('<div class="b-timeline" style="border-left:2px solid #8A7142;')
    assert '<b style="color:#C8A96B">09:45–10:30</b> · Standup' in html
    assert '<b style="color:#C8A96B">14:00–15:00</b> · Review' in html
    assert '<i style="color:#6b7280">10:30–14:00 · Free (3 h 30)</i>' in html
    # Chronological interleave: the 10:30 free slot sits between the events.
    assert html.index("Standup") < html.index("10:30–14:00") < html.index("Review")


def test_timeline_html_point_event_shows_start_only():
    html = timeline_html([_ev("Ping", _dt(9), _dt(9))], [], LABELS)
    assert '<b style="color:#C8A96B">09:00</b> · Ping' in html
    assert "09:00–" not in html


def test_timeline_html_unparseable_times_use_all_day_label():
    html = timeline_html([_ev("Conference", None, None), _ev("Call", _dt(9), _dt(10))], [], LABELS)
    assert '<b style="color:#C8A96B">All day</b> · Conference' in html
    # The no-time row pins to the top, before any timed row.
    assert html.index("Conference") < html.index("Call")


def test_timeline_html_skips_blank_titles():
    events = [_ev("", _dt(9), _dt(10)), _ev("   ", _dt(10), _dt(11)), _ev("Real", _dt(11), _dt(12))]
    html = timeline_html(events, [], LABELS)
    assert html.count("<div") == 2  # outer wrapper + the one real row
    assert "Real" in html


def test_timeline_html_all_blank_titles_is_empty():
    assert timeline_html([_ev("  ", _dt(9), _dt(10))], [], LABELS) == ""


def test_timeline_html_escapes_titles_and_labels():
    events = [_ev("Sync <b>danger</b> & co", _dt(9), _dt(10))]
    slots = [{"start": _dt(10), "end": _dt(11), "minutes": 60}]
    html = timeline_html(events, slots, {"free_slot": "<i>Libre</i>", "all_day": "Jour"})
    assert "Sync &lt;b&gt;danger&lt;/b&gt; &amp; co" in html
    assert "<b>danger</b>" not in html
    assert "&lt;i&gt;Libre&lt;/i&gt;" in html


def test_timeline_html_caps_at_16_rows_events_first():
    events = [_ev(f"E{i}", _dt(8, i), _dt(8, i + 1)) for i in range(18)]
    slots = [{"start": _dt(12), "end": _dt(13), "minutes": 60}]
    html = timeline_html(events, slots, LABELS)
    assert html.count("<div") == 17  # outer wrapper + 16 rows
    # Events outrank slots for the budget: the free slot did not make the cut.
    assert "Free" not in html
    # The cap keeps the earliest events.
    assert "E0" in html and "E15" in html and "E17" not in html


def test_timeline_html_slots_fill_remaining_budget():
    events = [_ev(f"E{i}", _dt(9 + i), _dt(9 + i, 30)) for i in range(3)]
    slots = free_slots(events, DAY)
    html = timeline_html(events, slots, LABELS)
    assert html.count("<div") == 1 + 3 + len(slots)
    assert "Free" in html


def test_timeline_html_converts_times_to_local_tz():
    """Events arriving in another zone render at their LOCAL_TZ wall time."""
    from datetime import timedelta, timezone

    other = timezone(_dt(9).utcoffset() + timedelta(hours=2))
    start, end = _dt(9).astimezone(other), _dt(10).astimezone(other)
    html = timeline_html([_ev("Shifted", start, end)], [], LABELS)
    assert "09:00–10:00" in html


def test_fmt_minutes_spellings():
    assert _fmt_minutes(50) == "50 min"
    assert _fmt_minutes(60) == "1 h"
    assert _fmt_minutes(65) == "1 h 05"
    assert _fmt_minutes(120) == "2 h"
    assert _fmt_minutes(210) == "3 h 30"
