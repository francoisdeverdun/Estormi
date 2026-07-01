"""_fetch_daily_actions — calendar/reminders/whatsapp for the briefing."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

import pytest

import estormi_briefing.day.day as day
import estormi_briefing.day.day_context as day_context
from estormi_briefing.day.day_context import _fetch_daily_actions

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def _pin_paris_tz(monkeypatch):
    """Pin the local timezone these assertions are written against.

    ``day.LOCAL_TZ`` is derived from the host's system clock, so the
    Paris-day window expectations below (e.g. 22:00 UTC == local midnight, a
    +2h summer offset) only hold on a Europe/Paris machine. Pinning it here
    makes the suite hermetic — it passes on UTC CI runners too.
    """
    monkeypatch.setattr(day, "LOCAL_TZ", ZoneInfo("Europe/Paris"))


# ── day_context: _fetch_daily_actions ────────────────────────────────────────


async def test_fetch_daily_actions_returns_dict_keys(actions_db):
    """Returns a dict with calendar and reminders keys (WhatsApp is no longer a
    precomputed action — the day-vision pulls recent conversation tails itself)."""

    db = actions_db

    result = await _fetch_daily_actions(db, date(2026, 5, 3))

    assert "calendar" in result
    assert "reminders" in result
    assert "whatsapp_pending" not in result
    assert result["calendar"] == []
    assert result["reminders"] == []


async def test_fetch_daily_actions_deduplicates_calendar_reminders(actions_db):
    """Reminder with same title as a calendar entry is excluded."""

    db = actions_db

    # Calendar all-day event at midnight UTC (2026-05-03T00:00:00+00:00)
    await db.execute(
        "INSERT INTO chunks (id, content_hash, source, title, date_ts, group_type) VALUES (?,?,?,?,?,?)",
        ("c1", "h1", "calendar", "Machine clair", "2026-05-03T00:00:00+00:00", "me"),
    )
    # Reminder for same event at 22:00 UTC previous day (= midnight Paris May 3)
    await db.execute(
        "INSERT INTO chunks (id, content_hash, source, title, date_ts) VALUES (?,?,?,?,?)",
        ("r1", "h2", "reminders", "Machine clair", "2026-05-02T22:00:00+00:00"),
    )
    await db.commit()

    result = await _fetch_daily_actions(db, date(2026, 5, 3))

    assert len(result["calendar"]) == 1
    assert result["calendar"][0]["title"] == "Machine clair"
    # Duplicate reminder must be suppressed
    assert all(r["title"] != "Machine clair" for r in result["reminders"])


async def test_fetch_daily_actions_uses_reminder_time_for_duplicate_calendar_bug(actions_db):
    """If Calendar has a stale shifted time, keep the matching Reminder time."""

    db = actions_db
    await db.execute(
        "INSERT INTO chunks (id, content_hash, source, title, date_ts, group_type) VALUES (?,?,?,?,?,?)",
        (
            "c1",
            "h1",
            "calendar",
            "Regarder AG copropriété",
            "2026-05-05T21:00:00+00:00",
            "me",
        ),
    )
    await db.execute(
        "INSERT INTO chunks (id, content_hash, source, title, date_ts) VALUES (?,?,?,?,?)",
        ("r1", "h2", "reminders", "Regarder AG copropriété", "2026-05-05T17:00:00+00:00"),
    )
    await db.commit()

    result = await _fetch_daily_actions(db, date(2026, 5, 5))

    assert len(result["calendar"]) == 1
    assert result["calendar"][0]["when"] == "19:00"
    assert result["calendar"][0]["source"] == "Calendar + Reminders"
    assert result["reminders"] == []


async def test_fetch_daily_actions_old_completed_reminder_does_not_mask_calendar(actions_db):
    """A reminder completed months ago must not hide a same-titled calendar
    occurrence today — the completed-mirror mask only looks back 7 days, or a
    recurring chore ("Take out the trash") would be masked forever."""

    db = actions_db
    await db.execute(
        "INSERT INTO chunks (id, content_hash, source, title, date_ts, completed) VALUES (?,?,?,?,?,?)",
        ("r1", "h1", "reminders", "Sortir les poubelles", "2026-03-10T18:00:00+00:00", 1),
    )
    await db.execute(
        "INSERT INTO chunks (id, content_hash, source, title, date_ts, group_type) VALUES (?,?,?,?,?,?)",
        ("c1", "h2", "calendar", "Sortir les poubelles", "2026-06-05T17:00:00+00:00", "me"),
    )
    await db.commit()

    result = await _fetch_daily_actions(db, date(2026, 6, 5))

    assert [a["title"] for a in result["calendar"]] == ["Sortir les poubelles"]


async def test_fetch_daily_actions_hides_completed_reminder_calendar_mirror(actions_db):
    """Completed Reminder mirrors in Calendar must not resurrect closed tasks."""

    db = actions_db
    await db.execute(
        "INSERT INTO chunks (id, content_hash, source, title, date_ts, completed) VALUES (?,?,?,?,?,?)",
        (
            "r1",
            "h1",
            "reminders",
            "Compostelle papa voiture et train",
            "2026-05-04T22:00:00+00:00",
            1,
        ),
    )
    await db.execute(
        "INSERT INTO chunks (id, content_hash, source, title, date_ts, group_type) VALUES (?,?,?,?,?,?)",
        (
            "c1",
            "h2",
            "calendar",
            "Compostelle papa voiture et train",
            "2026-05-05T08:00:00+00:00",
            "me",
        ),
    )
    await db.commit()

    result = await _fetch_daily_actions(db, date(2026, 5, 5))

    assert result["calendar"] == []
    assert result["reminders"] == []


async def test_fetch_daily_actions_ignores_shared_calendar_events(actions_db):
    """Shared/imported calendars are context, not day priorities."""

    db = actions_db
    await db.executemany(
        "INSERT INTO chunks (id, content_hash, source, title, date_ts, group_type) VALUES (?,?,?,?,?,?)",
        [
            ("c1", "h1", "calendar", "Shared event", "2026-05-05T17:00:00+00:00", "shared"),
            ("c2", "h2", "calendar", "Own event", "2026-05-05T18:00:00+00:00", "me"),
        ],
    )
    await db.commit()

    result = await _fetch_daily_actions(db, date(2026, 5, 5))

    assert [a["title"] for a in result["calendar"]] == ["Own event"]


async def test_fetch_daily_actions_overdue_reminder_flagged(actions_db):
    """Reminders due before today are included and flagged overdue=True."""

    db = actions_db

    # Overdue reminder: due April 28 (before May 3)
    await db.execute(
        "INSERT INTO chunks (id, content_hash, source, title, date_ts) VALUES (?,?,?,?,?)",
        ("r1", "h1", "reminders", "Tâche en retard", "2026-04-28T22:00:00+00:00"),
    )
    # Today's reminder (midnight Paris May 3 = 2026-05-02T22:00:00Z)
    await db.execute(
        "INSERT INTO chunks (id, content_hash, source, title, date_ts) VALUES (?,?,?,?,?)",
        ("r2", "h2", "reminders", "Tâche aujourd'hui", "2026-05-02T22:00:00+00:00"),
    )
    await db.commit()

    result = await _fetch_daily_actions(db, date(2026, 5, 3))

    titles = {r["title"]: r for r in result["reminders"]}
    assert "Tâche en retard" in titles
    assert titles["Tâche en retard"]["overdue"] is True
    assert "Tâche aujourd'hui" in titles
    assert titles["Tâche aujourd'hui"]["overdue"] is False


async def test_fetch_daily_actions_completed_reminders_hidden(actions_db):
    """Reminders with completed=1 must not appear in the daily note."""

    db = actions_db

    # Pending reminder — should appear
    await db.execute(
        "INSERT INTO chunks (id, content_hash, source, title, date_ts, completed) VALUES (?,?,?,?,?,?)",
        ("r1", "h1", "reminders", "Tâche active", "2026-05-03T07:00:00+00:00", 0),
    )
    # Completed reminder — must be hidden even though it's overdue
    await db.execute(
        "INSERT INTO chunks (id, content_hash, source, title, date_ts, completed) VALUES (?,?,?,?,?,?)",
        ("r2", "h2", "reminders", "Dossier investissement", "2026-04-28T22:00:00+00:00", 1),
    )
    await db.commit()

    result = await _fetch_daily_actions(db, date(2026, 5, 3))

    titles = [r["title"] for r in result["reminders"]]
    assert "Tâche active" in titles
    assert "Dossier investissement" not in titles, (
        "Completed reminders must not appear in the daily note"
    )


# NOTE: the WhatsApp pending-reply tests were removed when the precomputed
# pending-reply flag was retired. WhatsApp is no longer a precomputed daily
# action — the day-vision pulls recent conversation tails (see
# _fetch_recent_whatsapp in day_context) and judges actionability itself.


# ── day_context: multi-day calendar events ────────────────────────────────────


async def test_fetch_daily_actions_catches_multiday_event_spanning_today(actions_db):
    """Multi-day events that started before today but end after today must appear.

    Regression: Compostelle starts 2026-05-07, ends 2026-05-17. Running on
    2026-05-15 should find it because the event spans today.
    """

    db = actions_db

    # Compostelle: starts 2026-05-07T00:00:00Z, ends 2026-05-17T00:00:00Z
    # Running on 2026-05-15 → must appear
    await db.execute(
        "INSERT INTO chunks (id, content_hash, source, title, date, date_ts, end_date_ts, group_type) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "compostelle",
            "h_compostelle",
            "calendar",
            "Compostelle",
            "2026-05-07T00:00:00Z",
            "2026-05-07T00:00:00+00:00",
            "2026-05-17T00:00:00+00:00",
            "me",
        ),
    )
    # Another event fully outside the day — must NOT appear
    await db.execute(
        "INSERT INTO chunks (id, content_hash, source, title, date, date_ts, end_date_ts, group_type) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "other_trip",
            "h_other",
            "calendar",
            "Autre voyage",
            "2026-05-20T00:00:00Z",
            "2026-05-20T00:00:00+00:00",
            "2026-05-25T00:00:00+00:00",
            "me",
        ),
    )
    await db.commit()

    result = await _fetch_daily_actions(db, date(2026, 5, 15))

    titles = [a["title"] for a in result["calendar"]]
    assert "Compostelle" in titles, (
        f"Multi-day event 'Compostelle' spanning today must appear; got {titles}"
    )
    assert "Autre voyage" not in titles, (
        f"Future event 'Autre voyage' must not appear; got {titles}"
    )


async def test_fetch_daily_actions_multiday_event_without_end_date_ts(actions_db):
    """Events ingested before the end_date_ts fix (NULL end_date_ts) fall back
    to matching on date_ts alone (same-day behaviour)."""

    db = actions_db

    # Old-style chunk: no end_date_ts. Falls back to date_ts >= after behaviour.
    await db.execute(
        "INSERT INTO chunks (id, content_hash, source, title, date, date_ts, end_date_ts, group_type) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        # No end_date_ts — falls back to date_ts ≥ after behaviour.
        (
            "old_ev",
            "h_old",
            "calendar",
            "Old event same day",
            "2026-05-15T09:00:00Z",
            "2026-05-15T09:00:00+00:00",
            None,
            "me",
        ),
    )
    await db.commit()

    result = await _fetch_daily_actions(db, date(2026, 5, 15))

    titles = [a["title"] for a in result["calendar"]]
    assert "Old event same day" in titles, (
        f"Same-day event without end_date_ts must still appear; got {titles}"
    )


# ── day: _utc_bounds_for_local_day + Paris-day calendar logic ──


def test_daily_actions_utc_bounds_use_paris_day():
    from estormi_briefing.day.day import _utc_bounds_for_local_day

    after, before = _utc_bounds_for_local_day(date(2026, 5, 3))

    assert after == "2026-05-02T22:00:00+00:00"
    assert before == "2026-05-03T21:59:59.999999+00:00"


async def test_calendar_event_time_no_double_utc_offset(actions_db):
    """Regression: a 19:00 Paris event stored as 17:00Z must show '19:00'.

    The AppleScript exporter used to append 'Z' to local time (UTC+2 in summer),
    making Python add another +2h and display 21:00 instead of 19:00. After
    the fix, the exporter converts local→UTC before writing 'Z' and
    _format_action converts back to 19:00.
    """
    await actions_db.execute(
        "INSERT INTO chunks (id, content_hash, source, title, date_ts, group_type) "
        "VALUES ('ev1','hcal','calendar','AG copropriété','2026-05-05T17:00:00+00:00','me')"
    )
    await actions_db.commit()

    actions = await _fetch_daily_actions(actions_db, date(2026, 5, 5))
    assert actions["calendar"][0]["when"] == "19:00"


async def test_fetch_daily_actions_uses_paris_day_window(actions_db):
    """Paris-day window includes 00:00-22:59 UTC; 23:00 UTC is tomorrow."""
    await actions_db.executemany(
        "INSERT INTO chunks (id, content_hash, source, title, date_ts, group_type) VALUES (?,?,?,?,?,?)",
        [
            ("cal1", "h1", "calendar", "All-day event", "2026-05-03T00:00:00+00:00", "me"),
            ("rem1", "h2", "reminders", "Morning reminder", "2026-05-03T07:30:00+00:00", None),
            (
                "cal2",
                "h3",
                "calendar",
                "Tomorrow in Paris",
                "2026-05-03T22:00:00+00:00",
                "me",
            ),
        ],
    )
    await actions_db.commit()

    actions = await _fetch_daily_actions(actions_db, date(2026, 5, 3))

    assert [a["title"] for a in actions["calendar"]] == ["All-day event"]
    assert actions["calendar"][0]["when"] == "All day"
    assert [a["title"] for a in actions["reminders"]] == ["Morning reminder"]
    assert actions["reminders"][0]["when"] == "09:30"
    all_titles = [a["title"] for a in actions["calendar"] + actions["reminders"]]
    assert "Tomorrow in Paris" not in all_titles


async def test_calendar_local_midnight_renders_all_day(actions_db):
    """22:00 UTC on day N is midnight Paris on day N+1 — must render all-day."""
    await actions_db.execute(
        "INSERT INTO chunks (id, content_hash, source, title, date_ts, group_type) "
        "VALUES ('c1','h1','calendar','Machine foncé','2026-05-04T22:00:00+00:00','me')"
    )
    await actions_db.commit()
    result = await _fetch_daily_actions(actions_db, date(2026, 5, 5))
    assert result["calendar"][0]["when"] == "All day"


def test_local_when_label_midnight_local_is_next_day_all_day():
    """22:00Z is local midnight the next day (Paris) — labelled that local day,
    all-day, NOT the UTC day at 22:00 (the bug that produced 'demain soir')."""
    from estormi_briefing.day.day import _local_when_label

    assert _local_when_label("2026-06-01T22:00:00+00:00") == "2026-06-02 (Tuesday), all day"


def test_local_when_label_timed_event_in_local_time():
    from estormi_briefing.day.day import _local_when_label

    # 17:30Z → 19:30 Paris.
    assert _local_when_label("2026-05-31T17:30:00+00:00") == "2026-05-31 (Sunday) 19:30"


def test_local_when_label_blank_for_missing():
    from estormi_briefing.day.day import _local_when_label

    assert _local_when_label(None) == ""
    assert _local_when_label("") == ""


def test_day_anchor_names_today_and_following_days():
    from estormi_briefing.day.day import _day_anchor

    anchor = _day_anchor(date(2026, 5, 31))
    assert "Today is 2026-05-31 (Sunday)" in anchor
    assert "2026-06-01 (Monday)" in anchor
    assert "2026-06-02 (Tuesday)" in anchor


async def test_fetch_daily_actions_includes_gcal_events(actions_db):
    """Google Calendar events (source='gcal') reach My Day, not just Apple
    Calendar — a gcal-only user must still get a populated schedule."""
    # 19:30 Paris (17:30 UTC) on 2026-05-05 — a timed event in the 'couple' group.
    await actions_db.execute(
        "INSERT INTO chunks (id, content_hash, source, title, date_ts, group_type) "
        "VALUES ('g1','h1','gcal','Anniversaire maman','2026-05-05T17:30:00+00:00','couple')"
    )
    await actions_db.commit()
    result = await _fetch_daily_actions(actions_db, date(2026, 5, 5))
    assert [c["title"] for c in result["calendar"]] == ["Anniversaire maman"]
    # gcal rows are labelled Calendar (not Reminders) and carry the local time.
    assert result["calendar"][0]["source"] == "Calendar"
    assert result["calendar"][0]["when"] == "19:30"
    # tentative defaults False here — the "maybe" flag lives in the Qdrant text,
    # not this SQLite row, so it's enriched later in _generate_day_vision.
    assert result["calendar"][0]["tentative"] is False


# ── timezone-correct windowing (sweep 2 U7/U8/S1) ────────────────────────────
#
# A chunk's ``date_ts`` can carry a non-UTC offset (Google Calendar stores the
# raw RFC3339 dateTime, e.g. ``2026-06-05T23:30:00+02:00``). Comparing such
# offset-bearing strings *lexicographically* against a UTC bound mis-windows
# them, so the comparisons must normalise to the same instant first (SQLite
# ``datetime()`` for SQL, ``_parse_iso_datetime`` for Python). The ``_pin_paris_tz``
# autouse fixture above keeps these Paris-day expectations hermetic.


async def test_evening_gcal_event_with_local_offset_appears_in_calendar(actions_db):
    """U7: a gcal event at 23:30+02:00 (= 21:30Z, inside the Paris day) must appear.

    Pre-fix the calendar query compared ``date_ts <= before`` as raw strings:
    '2026-06-05T23:30:00+02:00' > before='2026-06-05T21:59:59.999999+00:00'
    lexicographically, so the true-21:30Z event was wrongly excluded. The SQLite
    ``datetime()`` normalisation lets it through.
    """
    await actions_db.execute(
        "INSERT INTO chunks (id, content_hash, source, title, date_ts, group_type) "
        "VALUES ('g1','h1','gcal','Dîner tardif','2026-06-05T23:30:00+02:00','me')"
    )
    await actions_db.commit()

    result = await _fetch_daily_actions(actions_db, date(2026, 6, 5))

    assert [c["title"] for c in result["calendar"]] == ["Dîner tardif"]


async def test_evening_offset_reminder_with_local_offset_appears(actions_db):
    """U7: a pending reminder at 23:30+02:00 (= 21:30Z, inside the Paris day)
    must appear — the reminder query had the same lexicographic ``date_ts <= ?``
    bug."""
    await actions_db.execute(
        "INSERT INTO chunks (id, content_hash, source, title, date_ts, completed) "
        "VALUES ('r1','h1','reminders','Sortir les poubelles','2026-06-05T23:30:00+02:00',0)"
    )
    await actions_db.commit()

    result = await _fetch_daily_actions(actions_db, date(2026, 6, 5))

    assert [r["title"] for r in result["reminders"]] == ["Sortir les poubelles"]


async def test_upcoming_events_excludes_past_offset_event():
    """U8: a genuinely-past event 2026-06-04T23:30:00+02:00 (= 21:30Z, before
    today's Paris-midnight bound 2026-06-04T22:00:00Z) must be EXCLUDED.

    Pre-fix the Python ``date_ts < after`` string compare kept it: the '+02:00'
    string is lexicographically NOT < after='2026-06-04T22:00:00+00:00', so a
    past event leaked into today..+HORIZON and seeded spurious correlations.
    A future event on the same offset must still pass.
    """

    async def _fake(payload, timeout=12.0):
        return [
            # 21:30Z on 2026-06-04 — strictly before the 2026-06-04T22:00:00Z
            # Paris-midnight bound for day 2026-06-05. Genuinely past.
            {"title": "Hier soir", "date_ts": "2026-06-04T23:30:00+02:00", "group_type": "me"},
            # Comfortably in the future — must survive.
            {"title": "Demain", "date_ts": "2026-06-06T10:00:00+02:00", "group_type": "me"},
        ]

    with patch.object(day_context, "_fetch_around_mcp", AsyncMock(side_effect=_fake)):
        out = await day_context._fetch_upcoming_events(date(2026, 6, 5))

    titles = [e["title"] for e in out]
    assert "Hier soir" not in titles
    assert titles == ["Demain"]


async def test_upcoming_events_sorts_by_instant_across_offsets():
    """U8: events with mixed offsets sort by real instant, not raw ISO text.

    20:00+02:00 (=18:00Z) precedes 19:00+00:00 (=19:00Z) — a string sort would
    invert them because '...20:00:00+02:00' > '...19:00:00+00:00'."""

    async def _fake(payload, timeout=12.0):
        return [
            {"title": "Later UTC", "date_ts": "2026-06-06T19:00:00+00:00", "group_type": "me"},
            {"title": "Earlier Paris", "date_ts": "2026-06-06T20:00:00+02:00", "group_type": "me"},
        ]

    with patch.object(day_context, "_fetch_around_mcp", AsyncMock(side_effect=_fake)):
        out = await day_context._fetch_upcoming_events(date(2026, 6, 5))

    # 18:00Z (Earlier Paris) before 19:00Z (Later UTC).
    assert [e["title"] for e in out] == ["Earlier Paris", "Later UTC"]
    # The internal sort key must not leak into the returned dicts.
    assert all("_dt" not in e for e in out)


async def test_recent_whatsapp_keeps_recent_offset_chunk():
    """S1: a WhatsApp chunk whose date_ts carries a non-UTC offset but is
    genuinely recent must survive the cutoff; a parseable stale one must drop,
    and an unparseable timestamp must be skipped defensively.

    Runs against *today's* briefing day: the recency cutoff anchors on the end
    of the briefing day, so now-relative chunks only make sense for a same-day
    run (the back-fill case has its own test below)."""
    now = datetime.now(timezone.utc)
    recent_paris = (now - timedelta(hours=2)).astimezone(ZoneInfo("Europe/Paris")).isoformat()
    stale_paris = (now - timedelta(hours=80)).astimezone(ZoneInfo("Europe/Paris")).isoformat()

    async def _fake(payload, timeout=12.0):
        return [
            {
                "source": "whatsapp",
                "text": "fresh",
                "group_type": "friends",
                "date_ts": recent_paris,
            },
            {
                "source": "whatsapp",
                "text": "stale",
                "group_type": "friends",
                "date_ts": stale_paris,
            },
            {
                "source": "whatsapp",
                "text": "junk",
                "group_type": "friends",
                "date_ts": "not-a-date",
            },
        ]

    with patch.object(day_context, "_fetch_around_mcp", AsyncMock(side_effect=_fake)):
        out = await day_context._fetch_recent_whatsapp(
            datetime.now(ZoneInfo("Europe/Paris")).date(), hours=48
        )

    assert [c["text"] for c in out] == ["fresh"]


async def test_recent_whatsapp_backfill_anchors_cutoff_on_briefing_day():
    """A re-built past day (ESTORMI_BRIEFING_DATE back-fill) must keep its
    WhatsApp tails: the recency cutoff anchors on the briefing day's END, not
    on wall-clock now() — which would empty the intersection and silently drop
    the WhatsApp section from the rebuilt briefing."""

    async def _fake(payload, timeout=12.0):
        return [
            # Inside the 48h leading up to the end of the 2026-06-05 Paris day.
            {
                "source": "whatsapp",
                "text": "in-window",
                "group_type": "friends",
                "date_ts": "2026-06-05T10:00:00+00:00",
            },
            # Before that 48h window — must still drop.
            {
                "source": "whatsapp",
                "text": "too-old",
                "group_type": "friends",
                "date_ts": "2026-06-02T10:00:00+00:00",
            },
        ]

    with patch.object(day_context, "_fetch_around_mcp", AsyncMock(side_effect=_fake)):
        out = await day_context._fetch_recent_whatsapp(date(2026, 6, 5), hours=48)

    assert [c["text"] for c in out] == ["in-window"]


async def test_health_chunks_excludes_next_day_labelled_cycle():
    """B1 (#2): a WHOOP ``date_ts`` is the cycle START — the evening BEFORE the
    labelled day. On a past-day rebuild the fetch window also returns the
    NEXT-day-labelled cycle (stamped ~22:xx of the briefing day itself); it must
    NOT become ``health[0]``. The labelled-briefing-day cycle (stamped the prior
    evening) leads, and the older trend cycle still rides in.

    Briefing day D = 2026-06-05 (Paris). Cycles labelled D-1/D/D+1 start on the
    evenings D-2/D-1/D respectively."""

    async def _fake(payload, timeout=12.0):
        return [
            # Labelled D+1 (2026-06-06): starts the evening of D → 2026-06-05T20:00Z.
            # >= the 2026-06-04T22:00Z Paris-midnight of day D → must be dropped.
            {"source": "whoop", "text": "cycle-Dplus1", "date_ts": "2026-06-05T20:00:00+00:00"},
            # Labelled D (2026-06-05): starts the evening of D-1 → 2026-06-04T20:00Z.
            # < day-start → kept, newest survivor → health[0].
            {"source": "whoop", "text": "cycle-D", "date_ts": "2026-06-04T20:00:00+00:00"},
            # Labelled D-1 (2026-06-04): starts the evening of D-2 → 2026-06-03T20:00Z.
            # Older trend row — must still ride in.
            {"source": "whoop", "text": "cycle-Dminus1", "date_ts": "2026-06-03T20:00:00+00:00"},
        ]

    with patch.object(day_context, "_fetch_around_mcp", AsyncMock(side_effect=_fake)):
        out = await day_context._fetch_health_chunks(date(2026, 6, 5))

    texts = [c["text"] for c in out]
    assert "cycle-Dplus1" not in texts
    assert texts[0] == "cycle-D"
    assert "cycle-Dminus1" in texts  # trend preserved


async def test_health_chunks_keeps_live_same_day_cycle():
    """B1 (#2): the live same-day case is unchanged. Today's real WHOOP cycle is
    stamped the prior evening (< today's local midnight), so it survives the
    day-start filter and leads."""

    async def _fake(payload, timeout=12.0):
        return [
            # Today's cycle (labelled 2026-06-05) — starts the evening before.
            {"source": "whoop", "text": "today", "date_ts": "2026-06-04T20:00:00+00:00"},
            {"source": "whoop", "text": "yesterday", "date_ts": "2026-06-03T20:00:00+00:00"},
        ]

    with patch.object(day_context, "_fetch_around_mcp", AsyncMock(side_effect=_fake)):
        out = await day_context._fetch_health_chunks(date(2026, 6, 5))

    assert [c["text"] for c in out] == ["today", "yesterday"]
