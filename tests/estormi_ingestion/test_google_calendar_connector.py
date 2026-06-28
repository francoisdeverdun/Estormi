"""Tests for the Google Calendar connector.

External dependencies (googleapiclient, keyring, google-auth) are mocked.
No network. No filesystem (other than tmp_path).

Markers are per-test: pure-logic helpers (``_looks_like_room_code``,
``_working_location``, ``_event_body``) are ``unit``; anything that drives the
mocked calendar API or the ``/ingest_chunk`` boundary — and especially the
partial-failure sync-token HOLD that guards against silent data loss — is
``integration``."""

from __future__ import annotations

import json
import sqlite3
from unittest.mock import MagicMock, patch

import pytest


def _make_db(tmp_path):
    db = sqlite3.connect(str(tmp_path / "estormi.db"))
    db.row_factory = sqlite3.Row
    # Mirror the production schema from estormi_server/sql/schema.py INIT_SQL so this
    # test catches column-shape drift instead of masking it.
    db.executescript(
        """
        CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE ingestion_watermarks (
            source          TEXT PRIMARY KEY,
            last_fetched_at TEXT,
            last_item_id    TEXT
        );
        CREATE TABLE chunks (
            id            TEXT PRIMARY KEY,
            content_hash  TEXT UNIQUE NOT NULL,
            source        TEXT,
            source_id     TEXT,
            title         TEXT,
            date          TEXT,
            date_ts       TEXT,
            end_date_ts   TEXT,
            group_type    TEXT,
            pending_reply INTEGER DEFAULT 0,
            chat_id_raw   TEXT,
            completed     INTEGER DEFAULT 0,
            ingested_at   TEXT DEFAULT (datetime('now'))
        );
        """
    )
    db.commit()
    return db


@pytest.mark.unit
def test_get_selected_calendar_ids_empty_means_all(tmp_path):
    from estormi_ingestion.google_calendar.sync import get_selected_calendar_ids

    db = _make_db(tmp_path)
    assert get_selected_calendar_ids(db) == []


@pytest.mark.unit
def test_get_selected_calendar_ids_returns_list(tmp_path):
    from estormi_ingestion.google_calendar.sync import get_selected_calendar_ids

    db = _make_db(tmp_path)
    db.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?)",
        ("google_calendar_selected_ids", json.dumps(["a@x", "b@y"])),
    )
    db.commit()
    assert get_selected_calendar_ids(db) == ["a@x", "b@y"]


class _FakeHttpError(Exception):
    def __init__(self):
        super().__init__("HTTP 410 Gone")
        self.resp = MagicMock(status=410)


@pytest.mark.unit
def test_410_gone_triggers_full_resync():
    """A 410 from the API must drop the stored sync token and retry full."""
    from estormi_ingestion.google_calendar import sync as gcal_sync

    call_count = {"n": 0}

    def fake_execute():
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise _FakeHttpError()
        return {"items": [], "nextSyncToken": "fresh"}

    events = MagicMock()
    events.list.return_value.execute.side_effect = fake_execute
    service = MagicMock()
    service.events.return_value = events

    ingested, deleted, errors, next_token = gcal_sync._sync_one_calendar(
        service, "cal-1", "stale-token"
    )

    assert call_count["n"] == 2  # full resync after 410
    assert next_token == "fresh"
    assert ingested == deleted == errors == 0
    # The first .list call should have used syncToken; the second must not.
    first_kwargs = events.list.call_args_list[0].kwargs
    second_kwargs = events.list.call_args_list[1].kwargs
    assert first_kwargs.get("syncToken") == "stale-token"
    assert "syncToken" not in second_kwargs
    assert second_kwargs.get("singleEvents") is True


@pytest.mark.unit
def test_cancelled_event_deletes_chunk():
    """A cancelled event is retracted via the /ingest_delete connector path."""
    from estormi_ingestion.google_calendar import sync as gcal_sync

    events = MagicMock()
    events.list.return_value.execute.return_value = {
        "items": [{"id": "evt-9", "status": "cancelled"}],
        "nextSyncToken": "tok2",
    }
    service = MagicMock()
    service.events.return_value = events

    with patch.object(gcal_sync, "_delete_event", return_value=gcal_sync.POST_INGESTED) as mock_del:
        ingested, deleted, errors, next_token = gcal_sync._sync_one_calendar(
            service, "cal-x", "tok1"
        )

    assert deleted == 1
    assert ingested == 0
    assert errors == 0
    mock_del.assert_called_once_with("evt-9")
    assert next_token == "tok2"


@pytest.mark.unit
def test_recurring_event_dedup():
    """Two instances of the same recurringEventId should be ingested once."""
    from estormi_ingestion.google_calendar import sync as gcal_sync

    events = MagicMock()
    events.list.return_value.execute.return_value = {
        "items": [
            {
                "id": "inst-1",
                "recurringEventId": "master-1",
                "summary": "Standup",
                "start": {"dateTime": "2026-01-01T09:00:00Z"},
                "end": {"dateTime": "2026-01-01T09:30:00Z"},
            },
            {
                "id": "inst-2",
                "recurringEventId": "master-1",
                "summary": "Standup",
                "start": {"dateTime": "2026-01-02T09:00:00Z"},
                "end": {"dateTime": "2026-01-02T09:30:00Z"},
            },
        ],
        "nextSyncToken": "tok",
    }
    service = MagicMock()
    service.events.return_value = events

    with patch.object(gcal_sync, "_post_event", return_value=gcal_sync.POST_INGESTED) as mock_post:
        ingested, _deleted, _errors, _next = gcal_sync._sync_one_calendar(service, "cal-y", None)

    assert ingested == 1
    assert mock_post.call_count == 1
    # The series is keyed on its master, not the per-instance id, so a resync
    # that keeps a different instance maps to the same chunk (no accumulation).
    assert mock_post.call_args.kwargs.get("source_id") == "master-1"


@pytest.mark.unit
def test_first_sync_bounds_window_and_orders():
    """The first (token-less) sync bounds the window and orders deterministically.

    No ``timeMax``/``orderBy`` is what let "repeats forever" series fan out into
    far-future instances and churn a new representative chunk per resync.
    """
    from estormi_ingestion.google_calendar import sync as gcal_sync

    events = MagicMock()
    events.list.return_value.execute.return_value = {"items": [], "nextSyncToken": "tok"}
    service = MagicMock()
    service.events.return_value = events

    gcal_sync._sync_one_calendar(service, "cal-z", None)  # None token → first sync

    kwargs = events.list.call_args_list[0].kwargs
    assert kwargs.get("singleEvents") is True
    assert kwargs.get("orderBy") == "startTime"
    assert "timeMin" in kwargs and "timeMax" in kwargs
    assert kwargs["timeMin"] < kwargs["timeMax"]


@pytest.mark.unit
def test_recurring_post_uses_master_source_id():
    """A recurring instance is posted with the master id as ``source_id``."""
    from estormi_ingestion.google_calendar import sync as gcal_sync

    captured = {}

    def fake_post_chunk(url, data, **kw):
        captured.update(data)
        resp = MagicMock()
        resp.json.return_value = {"status": "ok"}
        resp.raise_for_status.return_value = None
        return resp

    event = {
        "id": "master-7_20380520",
        "recurringEventId": "master-7",
        "summary": "Weekly sync",
        "start": {"dateTime": "2038-05-20T09:00:00Z"},
        "end": {"dateTime": "2038-05-20T09:30:00Z"},
    }
    with patch.object(gcal_sync, "post_chunk", side_effect=fake_post_chunk):
        gcal_sync._post_event(event, "cal@x", "work", source_id="master-7")

    assert captured["source_id"] == "master-7"
    assert captured["source"] == "gcal"


@pytest.mark.unit
def test_load_group_types_parses_setting(tmp_path):
    """The {calendar_id: group_type} map is read from the settings blob."""
    from estormi_ingestion.google_calendar import sync as gcal_sync

    db = _make_db(tmp_path)
    db.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?)",
        ("google_calendar_group_types", json.dumps({"work@x": "work", "fam@y": "family"})),
    )
    db.commit()
    assert gcal_sync._load_group_types(db) == {"work@x": "work", "fam@y": "family"}


@pytest.mark.unit
def test_load_group_types_missing_is_empty(tmp_path):
    from estormi_ingestion.google_calendar import sync as gcal_sync

    db = _make_db(tmp_path)
    assert gcal_sync._load_group_types(db) == {}


@pytest.mark.unit
def test_post_event_stamps_calendar_id_and_group_type():
    """Each event chunk must carry chat_id_raw + group_type so it can be
    retroactively re-tagged when the calendar's group_type changes."""
    from estormi_ingestion.google_calendar import sync as gcal_sync

    event = {
        "id": "evt-1",
        "summary": "Sprint review",
        "start": {"dateTime": "2026-02-01T10:00:00Z"},
        "end": {"dateTime": "2026-02-01T11:00:00Z"},
    }
    captured: dict = {}

    class _Resp:
        status_code = 200

        @staticmethod
        def json():
            return {"status": "ok"}

        @staticmethod
        def raise_for_status():
            return None

    def fake_post(url, payload, **kwargs):
        captured["json"] = payload
        return _Resp()

    # The connector now goes through shared.http_client.post_chunk (so a
    # transient 5xx from MCP retries before silently dropping the event +
    # advancing the syncToken past it). The mock keeps the same shape: URL +
    # JSON payload + keyword args.
    with patch.object(gcal_sync, "post_chunk", side_effect=fake_post):
        outcome = gcal_sync._post_event(event, "team@group.calendar.google.com", "work")

    assert outcome == gcal_sync.POST_INGESTED
    assert captured["json"]["source"] == "gcal"
    assert captured["json"]["chat_id_raw"] == "team@group.calendar.google.com"
    assert captured["json"]["group_type"] == "work"


# ── working location + tentative status (auto-copy calendar signals) ─────────


@pytest.mark.unit
def test_working_location_combines_label_and_type():
    """A homeOffice/officeLocation/customLocation type + human label render as
    '<label> (<human type>)'."""
    from estormi_ingestion.google_calendar import sync as gcal_sync

    def wl(kind, label):
        return gcal_sync._working_location(
            {
                "extendedProperties": {
                    "private": {
                        "dayWorkingLocationType": kind,
                        "dayWorkingLocationLabel": label,
                    }
                }
            }
        )

    assert wl("homeOffice", "Télétravail") == "Télétravail (home office)"
    assert wl("officeLocation", "FR-DigitalFactory") == "FR-DigitalFactory (office)"
    # No properties at all → empty (the day had no work location set).
    assert gcal_sync._working_location({}) == ""
    # Only a label, unknown/absent type → the label still carries through.
    assert wl("", "FR-HO") == "FR-HO"


@pytest.mark.unit
def test_event_body_carries_no_status_or_location_trailer():
    """The 'maybe' RSVP and working location are now structured chunk fields,
    not text trailers — the body is the plain title/time/location/description
    shape, with the location on the line right after the '→' time line (what
    run_knowledge._parse_event_location reads)."""
    from estormi_ingestion.google_calendar import sync as gcal_sync

    event = {
        "id": "evt-2",
        "summary": "Sprint review",
        "location": "Paris HQ",
        "status": "tentative",
        "start": {"dateTime": "2026-02-01T10:00:00Z"},
        "end": {"dateTime": "2026-02-01T11:00:00Z"},
        "description": "agenda",
        "extendedProperties": {
            "private": {
                "dayWorkingLocationType": "homeOffice",
                "dayWorkingLocationLabel": "Télétravail",
            }
        },
    }
    title, start, end, text = gcal_sync._event_body(event)
    assert (title, start, end) == ("Sprint review", "2026-02-01T10:00:00Z", "2026-02-01T11:00:00Z")
    lines = text.split("\n")
    arrow_idx = next(i for i, ln in enumerate(lines) if "→" in ln)
    assert lines[arrow_idx + 1] == "Paris HQ"
    assert "Status:" not in text
    assert "Working location:" not in text
    assert text.endswith("agenda")


@pytest.mark.unit
def test_event_body_all_day_end_is_inclusive():
    """Google's all-day ``end.date`` is EXCLUSIVE: a one-day event on the 15th
    carries ``end.date == '2026-02-16'``. _event_body must decrement it to the
    last covered day so the stored end_date_ts is inclusive (bare YYYY-MM-DD),
    otherwise every all-day event over-includes by one day into the next local
    day's briefing/retrieval window. Timed events keep their real instant end."""
    from estormi_ingestion.google_calendar import sync as gcal_sync

    # One-day all-day event on 2026-02-15 (Google exclusive end 2026-02-16).
    _, start, end, _ = gcal_sync._event_body(
        {
            "id": "ad-1",
            "summary": "Holiday",
            "start": {"date": "2026-02-15"},
            "end": {"date": "2026-02-16"},
        }
    )
    assert (start, end) == ("2026-02-15", "2026-02-15")

    # Multi-day all-day event covering 15→17 (Google exclusive end 2026-02-18).
    _, start2, end2, _ = gcal_sync._event_body(
        {
            "id": "ad-2",
            "summary": "Trip",
            "start": {"date": "2026-02-15"},
            "end": {"date": "2026-02-18"},
        }
    )
    assert (start2, end2) == ("2026-02-15", "2026-02-17")

    # Timed events are untouched — the end is a real instant, not decremented.
    _, _, end3, _ = gcal_sync._event_body(
        {
            "id": "t-1",
            "summary": "Sync",
            "start": {"dateTime": "2026-02-01T10:00:00Z"},
            "end": {"dateTime": "2026-02-01T11:00:00Z"},
        }
    )
    assert end3 == "2026-02-01T11:00:00Z"


@pytest.mark.unit
def test_post_event_sends_structured_calendar_fields(monkeypatch):
    """A 'maybe' RSVP, the Google eventType, the working location, and the end
    timestamp all ride to /ingest_chunk as structured fields (so the Briefing
    reads them as columns, not by parsing the chunk text)."""
    from estormi_ingestion.google_calendar import sync as gcal_sync

    event = {
        "id": "evt-oo",
        "summary": "Public holiday",
        "status": "tentative",
        "eventType": "outOfOffice",
        "start": {"dateTime": "2026-02-01T10:00:00Z"},
        "end": {"dateTime": "2026-02-01T11:00:00Z"},
        "extendedProperties": {
            "private": {
                "dayWorkingLocationType": "homeOffice",
                "dayWorkingLocationLabel": "Télétravail",
            }
        },
    }
    captured: dict = {}

    class _Resp:
        status_code = 200

        @staticmethod
        def json():
            return {"status": "ok"}

        @staticmethod
        def raise_for_status():
            return None

    monkeypatch.setattr(
        gcal_sync, "post_chunk", lambda url, payload, **kw: captured.update(json=payload) or _Resp()
    )
    outcome = gcal_sync._post_event(event, "me@gmail.com", "work")

    assert outcome == gcal_sync.POST_INGESTED
    body = captured["json"]
    assert body["event_type"] == "outOfOffice"
    assert body["event_status"] == "tentative"
    assert body["working_location"] == "Télétravail (home office)"
    assert body["end_date_ts"] == "2026-02-01T11:00:00Z"


@pytest.mark.unit
def test_post_event_structured_field_defaults(monkeypatch):
    """An ordinary event with no eventType / RSVP / working location sends the
    documented defaults — never a missing key."""
    from estormi_ingestion.google_calendar import sync as gcal_sync

    event = {
        "id": "evt-4",
        "summary": "Standup",
        "location": "Room A",
        "start": {"dateTime": "2026-02-01T09:00:00Z"},
        "end": {"dateTime": "2026-02-01T09:15:00Z"},
        "description": "daily",
    }
    captured: dict = {}

    class _Resp:
        status_code = 200

        @staticmethod
        def json():
            return {"status": "ok"}

        @staticmethod
        def raise_for_status():
            return None

    monkeypatch.setattr(
        gcal_sync, "post_chunk", lambda url, payload, **kw: captured.update(json=payload) or _Resp()
    )
    gcal_sync._post_event(event, "me@gmail.com", "work")

    body = captured["json"]
    assert body["event_type"] == "default"
    assert body["event_status"] == "confirmed"
    assert body["working_location"] == ""


@pytest.mark.unit
def test_cancelled_recurring_instance_deletes_master_not_instance():
    """Bug U5: a cancelled recurring instance must retract via the MASTER id.

    Chunks are stored under the master (``recurringEventId``), so the deletion
    key must resolve the same way ingestion stored it
    (``event.get("recurringEventId") or event_id``). Previously ``_delete_event``
    was called with the per-instance id 'master-1_20260612T100000Z', which
    matched nothing in the DB and left the chunk alive; after the fix it is
    called with 'master-1'."""
    from estormi_ingestion.google_calendar import sync as gcal_sync

    cancelled_instance = {
        "id": "master-1_20260612T100000Z",
        "status": "cancelled",
        "recurringEventId": "master-1",
    }

    events = MagicMock()
    events.list.return_value.execute.return_value = {
        "items": [cancelled_instance],
        "nextSyncToken": "tok-after",
    }
    service = MagicMock()
    service.events.return_value = events

    with patch.object(gcal_sync, "_delete_event", return_value=gcal_sync.POST_INGESTED) as mock_del:
        ingested, deleted, errors, next_token = gcal_sync._sync_one_calendar(
            service, "cal-recurring", "sync-tok"
        )

    # Deletion must have been triggered once and with the MASTER id.
    mock_del.assert_called_once_with("master-1")
    assert deleted == 1
    assert ingested == 0
    assert errors == 0
    assert next_token == "tok-after"


# ── _looks_like_room_code: corporate desk/room codes are not real places ─────


@pytest.mark.unit
@pytest.mark.parametrize(
    "location",
    [
        "US-HQ-5-OPEN SPACE West",  # site prefix US- + open space
        "GB-Office2 - Floor 4 Desk 12",  # site prefix GB-
        "FR-DigitalFactory",  # bare site prefix
        "Building 45-4-L",  # numeric desk segment 45-4-L
        "Acme Open  Space East",  # 'open space' phrase, any casing/spacing
        "the OPEN SPACE",
    ],
)
def test_looks_like_room_code_true_for_desk_identifiers(location):
    """Room/desk booking codes are recognised so they stay out of chunk text."""
    from estormi_ingestion.google_calendar.sync import _looks_like_room_code

    assert _looks_like_room_code(location) is True


@pytest.mark.unit
@pytest.mark.parametrize(
    "location",
    [
        "",  # empty
        "   ",  # whitespace only
        "Paris HQ",  # a real place
        "Café de Flore, Paris",
        "https://meet.example.com/abc-defg-hij",  # a meeting URL, not a desk
        "Room A",  # plain room name, no site prefix / numeric desk / 'open space'
        "us-hq",  # lowercase site prefix does NOT match the [A-Z]{2}- rule
    ],
)
def test_looks_like_room_code_false_for_real_locations(location):
    """A genuine place (or nothing) is not mistaken for a desk code."""
    from estormi_ingestion.google_calendar.sync import _looks_like_room_code

    assert _looks_like_room_code(location) is False


# ── sync(): the partial-failure sync-token HOLD (data-loss guard) ────────────


def _patched_sync(monkeypatch, db, *, calendars, per_calendar):
    """Run gcal_sync.sync(db) with creds + service stubbed.

    ``calendars`` is the calendar-id list the (stubbed) ``get_selected_calendar_ids``
    returns; ``per_calendar`` maps each id to the
    ``(ingested, deleted, errors, next_token)`` tuple its ``_sync_one_calendar``
    should yield. Returns the counts dict from ``sync``.
    """
    from estormi_ingestion.google_calendar import sync as gcal_sync

    monkeypatch.setattr(gcal_sync.gcal_auth, "get_credentials", lambda: object())
    monkeypatch.setattr(gcal_sync, "_build_service", lambda creds: object())
    monkeypatch.setattr(gcal_sync, "get_selected_calendar_ids", lambda _db: list(calendars))

    def fake_sync_one(_service, cal_id, _token, _group_type):
        return per_calendar[cal_id]

    monkeypatch.setattr(gcal_sync, "_sync_one_calendar", fake_sync_one)
    return gcal_sync.sync(db=db)


@pytest.mark.integration
def test_sync_token_held_when_page_partially_fails(tmp_path, monkeypatch):
    """The DATA-LOSS GUARD: a calendar whose window had a POST error must keep
    its OLD sync token. Advancing Google's delta cursor past events we never
    persisted is silent permanent loss — the next syncToken pull only returns
    changes since the saved token, so a dropped event is never re-delivered."""
    from estormi_ingestion.google_calendar import sync as gcal_sync

    db = _make_db(tmp_path)
    # The calendar already has a stored token from a prior clean run.
    db.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?)",
        (gcal_sync.SETTING_SYNC_TOKEN, json.dumps({"cal-1": "OLD-TOKEN"})),
    )
    db.commit()

    counts = _patched_sync(
        monkeypatch,
        db,
        calendars=["cal-1"],
        # Google handed back a fresh token, but one event in the page errored.
        per_calendar={"cal-1": (2, 0, 1, "NEW-TOKEN")},
    )

    assert counts["errors"] == 1
    assert counts["ingested"] == 2
    # The guard: the stored token is NOT advanced to NEW-TOKEN — it is held at
    # OLD-TOKEN so the failed window is re-pulled on the next run.
    stored = json.loads(gcal_sync._setting_get(db, gcal_sync.SETTING_SYNC_TOKEN))
    assert stored["cal-1"] == "OLD-TOKEN"


@pytest.mark.integration
def test_sync_token_advances_on_clean_page(tmp_path, monkeypatch):
    """The other half of the guard: with zero errors the fresh token IS saved,
    so the next run pulls only deltas instead of re-walking the window."""
    from estormi_ingestion.google_calendar import sync as gcal_sync

    db = _make_db(tmp_path)
    db.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?)",
        (gcal_sync.SETTING_SYNC_TOKEN, json.dumps({"cal-1": "OLD-TOKEN"})),
    )
    db.commit()

    counts = _patched_sync(
        monkeypatch,
        db,
        calendars=["cal-1"],
        per_calendar={"cal-1": (3, 1, 0, "NEW-TOKEN")},
    )

    assert counts["errors"] == 0
    stored = json.loads(gcal_sync._setting_get(db, gcal_sync.SETTING_SYNC_TOKEN))
    assert stored["cal-1"] == "NEW-TOKEN"


def _gcal_watermark(db):
    """Return the gcal ``last_fetched_at`` stamp, or None if never stamped."""
    cur = db.execute("SELECT last_fetched_at FROM ingestion_watermarks WHERE source = 'gcal'")
    row = cur.fetchone()
    return row[0] if row else None


@pytest.mark.integration
def test_clean_sync_stamps_freshness_watermark(tmp_path, monkeypatch):
    """A clean run records WHEN gcal last synced in ingestion_watermarks, so the
    SourcesPanel row shows a real date instead of the "sync tokens" placeholder."""
    import re

    db = _make_db(tmp_path)

    counts = _patched_sync(
        monkeypatch,
        db,
        calendars=["cal-1"],
        per_calendar={"cal-1": (3, 0, 0, "NEW-TOKEN")},
    )

    assert counts["errors"] == 0
    stamp = _gcal_watermark(db)
    # ISO-Z form (YYYY-MM-DDTHH:MM:SSZ) — what the SPA compacts to MM-DD HH:MM.
    assert stamp is not None
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", stamp)


@pytest.mark.integration
def test_clean_sync_stamps_even_without_a_returned_token(tmp_path, monkeypatch):
    """The real gcal path: the first-sync request carries timeMin/timeMax/orderBy,
    so Google suppresses nextSyncToken and ``_sync_one_calendar`` returns None for
    it. A clean run must STILL stamp the freshness date — gating on a token advance
    would leave the column perpetually blank (the live-observed failure)."""
    db = _make_db(tmp_path)

    counts = _patched_sync(
        monkeypatch,
        db,
        calendars=["cal-1"],
        per_calendar={"cal-1": (0, 0, 0, None)},  # clean, but no token returned
    )

    assert counts["errors"] == 0
    assert _gcal_watermark(db) is not None


@pytest.mark.integration
def test_failed_sync_leaves_watermark_unstamped(tmp_path, monkeypatch):
    """A run with any error must not stamp a freshness date — the calendars are
    not confirmed current, so the date is held back for a clean retry."""
    db = _make_db(tmp_path)

    counts = _patched_sync(
        monkeypatch,
        db,
        calendars=["cal-1"],
        per_calendar={"cal-1": (0, 0, 1, "NEW-TOKEN")},  # POST error in the window
    )

    assert counts["errors"] == 1
    assert _gcal_watermark(db) is None


@pytest.mark.integration
def test_sync_holds_one_token_but_advances_another(tmp_path, monkeypatch):
    """Per-calendar isolation: a partial failure on one calendar must not stall
    the cursor of a sibling calendar that synced cleanly in the same run."""
    from estormi_ingestion.google_calendar import sync as gcal_sync

    db = _make_db(tmp_path)
    db.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?)",
        (gcal_sync.SETTING_SYNC_TOKEN, json.dumps({"bad": "OLD-BAD", "good": "OLD-GOOD"})),
    )
    db.commit()

    counts = _patched_sync(
        monkeypatch,
        db,
        calendars=["bad", "good"],
        per_calendar={
            "bad": (1, 0, 2, "NEW-BAD"),  # 2 POST errors → hold
            "good": (4, 0, 0, "NEW-GOOD"),  # clean → advance
        },
    )

    assert counts["calendars"] == 2
    assert counts["ingested"] == 5
    assert counts["errors"] == 2
    stored = json.loads(gcal_sync._setting_get(db, gcal_sync.SETTING_SYNC_TOKEN))
    assert stored["bad"] == "OLD-BAD"  # held
    assert stored["good"] == "NEW-GOOD"  # advanced


@pytest.mark.integration
def test_sync_counts_aggregate_across_calendars(tmp_path, monkeypatch):
    """sync() sums ingested/deleted/errors and counts calendars across the set."""
    db = _make_db(tmp_path)

    counts = _patched_sync(
        monkeypatch,
        db,
        calendars=["a", "b", "c"],
        per_calendar={
            "a": (2, 1, 0, "ta"),
            "b": (3, 0, 0, "tb"),
            "c": (0, 2, 1, "tc"),
        },
    )

    assert counts == {"ingested": 5, "deleted": 3, "calendars": 3, "errors": 1}


@pytest.mark.integration
def test_sync_raised_calendar_counts_as_error_without_aborting_run(tmp_path, monkeypatch):
    """A calendar whose sync RAISES (e.g. a second consecutive 410) is counted
    as one error and the run continues to the next calendar — the bad one keeps
    its old token because no fresh token was returned for it."""
    from estormi_ingestion.google_calendar import sync as gcal_sync

    db = _make_db(tmp_path)
    db.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?)",
        (gcal_sync.SETTING_SYNC_TOKEN, json.dumps({"boom": "OLD"})),
    )
    db.commit()

    monkeypatch.setattr(gcal_sync.gcal_auth, "get_credentials", lambda: object())
    monkeypatch.setattr(gcal_sync, "_build_service", lambda creds: object())
    monkeypatch.setattr(gcal_sync, "get_selected_calendar_ids", lambda _db: ["boom", "ok"])

    def fake_sync_one(_service, cal_id, _token, _group_type):
        if cal_id == "boom":
            raise RuntimeError("410 Gone (second consecutive)")
        return (1, 0, 0, "OK-NEW")

    monkeypatch.setattr(gcal_sync, "_sync_one_calendar", fake_sync_one)

    counts = gcal_sync.sync(db=db)

    assert counts["errors"] == 1
    assert counts["ingested"] == 1
    assert counts["calendars"] == 1  # only the calendar that didn't raise
    stored = json.loads(gcal_sync._setting_get(db, gcal_sync.SETTING_SYNC_TOKEN))
    assert stored["boom"] == "OLD"  # the raised calendar's token is untouched
    assert stored["ok"] == "OK-NEW"


@pytest.mark.integration
def test_sync_no_credentials_returns_single_error(tmp_path, monkeypatch):
    """No usable Google credentials short-circuits to a single counted error
    (the pipeline stage then fails loudly instead of looking clean-but-empty)."""
    from estormi_ingestion.google_calendar import sync as gcal_sync

    db = _make_db(tmp_path)
    monkeypatch.setattr(gcal_sync.gcal_auth, "get_credentials", lambda: None)

    counts = gcal_sync.sync(db=db)

    assert counts == {"ingested": 0, "deleted": 0, "calendars": 0, "errors": 1}


# ── _main(): exit-code contract ──────────────────────────────────────────────


@pytest.mark.integration
def test_main_returns_reauth_exit_code_when_token_revoked(monkeypatch, capsys):
    """A stored-but-dead refresh token (load_token present, get_credentials None)
    surfaces as the reserved re-auth exit code 2 — distinct from a transient
    API error (exit 1) — so a future UI can tell 'reconnect' from 'retry'."""
    from estormi_ingestion.google_calendar import sync as gcal_sync

    monkeypatch.setattr(gcal_sync.gcal_auth, "load_token", lambda: {"refresh_token": "dead"})
    monkeypatch.setattr(gcal_sync.gcal_auth, "get_credentials", lambda: None)

    def must_not_run(*_a, **_k):
        raise AssertionError("sync() must not run once re-auth is detected")

    monkeypatch.setattr(gcal_sync, "sync", must_not_run)

    assert gcal_sync._main() == gcal_sync._EXIT_REAUTH == 2
    assert "re-authenticate" in capsys.readouterr().out


@pytest.mark.integration
def test_main_returns_one_on_sync_errors(monkeypatch):
    """When credentials are fine but the sync reports errors, _main exits 1."""
    from estormi_ingestion.google_calendar import sync as gcal_sync

    monkeypatch.setattr(gcal_sync.gcal_auth, "load_token", lambda: None)
    monkeypatch.setattr(
        gcal_sync, "sync", lambda: {"calendars": 1, "ingested": 0, "deleted": 0, "errors": 2}
    )
    assert gcal_sync._main() == 1


@pytest.mark.integration
def test_main_returns_zero_on_clean_sync(monkeypatch):
    """A clean sync (no errors) exits 0 so the pipeline stage is marked green."""
    from estormi_ingestion.google_calendar import sync as gcal_sync

    monkeypatch.setattr(gcal_sync.gcal_auth, "load_token", lambda: None)
    monkeypatch.setattr(
        gcal_sync, "sync", lambda: {"calendars": 2, "ingested": 5, "deleted": 0, "errors": 0}
    )
    assert gcal_sync._main() == 0
