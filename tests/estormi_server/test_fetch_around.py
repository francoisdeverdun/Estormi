"""fetch_around — time-window retrieval across sources (the correlation primitive)."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException

from estormi_server.storage.search_api import fetch_around
from estormi_server.storage.writers import ingest_chunk

pytestmark = pytest.mark.integration


def _point(chunk_id: str, text: str) -> MagicMock:
    p = MagicMock()
    p.id = chunk_id
    p.payload = {"text": text}
    return p


class TestFetchAround:
    @pytest.fixture(autouse=True)
    def _wire(self, wired_tools_db):
        pass

    async def _seed(self, mock_qdrant):
        """Three sources clustered on 2026-05-20, one far-off outlier."""
        await ingest_chunk(
            text="Dinner Thursday 8pm",
            source="calendar",
            content_hash="cal-0",
            source_id="evt-1",
            date="2026-05-20T20:00:00Z",
        )
        await ingest_chunk(
            text="bring the wine",
            source="whatsapp",
            content_hash="wa-0",
            source_id="wa-1",
            date="2026-05-20T18:00:00Z",
        )
        await ingest_chunk(
            text="reminder buy wine",
            source="reminders",
            content_hash="rem-0",
            source_id="rem-1",
            date="2026-05-19T09:00:00Z",
        )
        await ingest_chunk(
            text="ancient history",
            source="mail",
            content_hash="old-0",
            source_id="m-1",
            date="2026-01-01T09:00:00Z",
        )

    async def test_window_clusters_cross_source(self, db, mock_qdrant):
        await self._seed(mock_qdrant)
        out = await fetch_around("2026-05-20", window_days=1)
        sources = {c["source"] for c in out}
        # The three same-week items across three sources are returned together;
        # the January outlier is outside the ±1d window.
        assert sources == {"calendar", "whatsapp", "reminders"}
        # Newest first.
        assert out[0]["date_ts"] >= out[-1]["date_ts"]

    async def test_window_excludes_outlier(self, db, mock_qdrant):
        await self._seed(mock_qdrant)
        out = await fetch_around("2026-05-20", window_days=1)
        assert all(c["source"] != "mail" for c in out)

    async def test_forward_days_bounds_lookahead(self, db, mock_qdrant):
        """``forward_days`` caps the look-ahead independently of the look-back:
        a backward window that would symmetrically pull tomorrow's chunks stops
        at the centre day when forward_days=0 — the fix for next-day leakage in
        the briefing's day-context."""
        await ingest_chunk(
            text="yesterday",
            source="mail",
            content_hash="f-y",
            source_id="f-y",
            date="2026-05-19T09:00:00Z",
        )
        await ingest_chunk(
            text="today",
            source="calendar",
            content_hash="f-t",
            source_id="f-t",
            date="2026-05-20T12:00:00Z",
        )
        await ingest_chunk(
            text="tomorrow event",
            source="calendar",
            content_hash="f-tm",
            source_id="f-tm",
            date="2026-05-21T12:00:00Z",
        )
        # Symmetric ±2 includes tomorrow…
        sym = {c["source_id"] for c in await fetch_around("2026-05-20", window_days=2)}
        assert "f-tm" in sym
        # …but forward_days=0 keeps the look-back and today, dropping tomorrow.
        bounded = {
            c["source_id"] for c in await fetch_around("2026-05-20", window_days=2, forward_days=0)
        }
        assert "f-y" in bounded and "f-t" in bounded
        assert "f-tm" not in bounded

    async def test_bare_date_window_anchored_on_local_day(self, db, mock_qdrant, monkeypatch):
        """A bare ``date`` names a LOCAL calendar day, so the window is anchored on
        that day's edges (ESTORMI_LOCAL_TZ), not UTC midnight. Pin Paris (+02:00):
        fetch_around('2026-05-20', window_days=1, forward_days=0) must cover the
        whole of LOCAL 2026-05-20 — a 23:30 Paris (= 21:30Z) evening chat is IN —
        while the first minutes of LOCAL 2026-05-21 (00:30 Paris = 2026-05-20T22:30Z)
        must NOT leak in. Under the old UTC anchoring the window ended at
        2026-05-21T00:00Z, so the 22:30Z next-day chunk wrongly leaked and the
        symptom was 'the briefing mixes current & next day'.
        """
        monkeypatch.setenv("ESTORMI_LOCAL_TZ", "Europe/Paris")
        # 23:30 local on the briefing day → 21:30Z, inside local 2026-05-20.
        await ingest_chunk(
            text="tonight in Paris",
            source="whatsapp",
            content_hash="ld-evening",
            source_id="ld-evening",
            date="2026-05-20T23:30:00+02:00",
        )
        # 00:30 local the NEXT day → 22:30Z, the leak forward_days=0 must reject.
        await ingest_chunk(
            text="after midnight in Paris (next local day)",
            source="whatsapp",
            content_hash="ld-leak",
            source_id="ld-leak",
            date="2026-05-21T00:30:00+02:00",
        )
        out = await fetch_around("2026-05-20", window_days=1, forward_days=0)
        keys = {c["source_id"] for c in out}
        assert "ld-evening" in keys  # today's local evening is covered
        assert "ld-leak" not in keys  # next local day does not leak in

    async def test_source_filter(self, db, mock_qdrant):
        await self._seed(mock_qdrant)
        out = await fetch_around("2026-05-20", window_days=2, sources=["calendar"])
        assert [c["source"] for c in out] == ["calendar"]

    async def test_corpus_filter(self, db, mock_qdrant):
        # news → world corpus; mail → personal corpus (derived in ingest_chunk).
        await ingest_chunk(
            text="market moves",
            source="news",
            content_hash="news-0",
            source_id="n-1",
            date="2026-05-20T07:00:00Z",
        )
        await ingest_chunk(
            text="personal note",
            source="mail",
            content_hash="mail-0",
            source_id="p-1",
            date="2026-05-20T07:00:00Z",
        )
        world = await fetch_around("2026-05-20", window_days=1, corpus="world")
        personal = await fetch_around("2026-05-20", window_days=1, corpus="personal")
        assert {c["source"] for c in world} == {"news"}
        assert {c["source"] for c in personal} == {"mail"}

    async def test_text_hydrated_from_qdrant(self, db, mock_qdrant):
        r = await ingest_chunk(
            text="bring the wine",
            source="whatsapp",
            content_hash="wa-x",
            source_id="wa-x",
            date="2026-05-20T18:00:00Z",
        )
        mock_qdrant.retrieve.return_value = [_point(r["id"], "bring the wine")]
        out = await fetch_around("2026-05-20", window_days=1)
        assert out[0]["text"] == "bring the wine"

    async def test_unparseable_date_raises_400(self, db, mock_qdrant):
        # 'date' is required by the inputSchema; an unparseable value is a
        # client error (HTTP 400), not a genuinely-empty window (which still
        # returns []). See sweep2 bug U18.
        await self._seed(mock_qdrant)
        with pytest.raises(HTTPException) as exc:
            await fetch_around("not-a-date")
        assert exc.value.status_code == 400

    async def test_timezone_offset_overlap_uses_real_instant(self, db, mock_qdrant):
        """A chunk stored with a non-UTC offset (Google Calendar feeds e.g.
        ``+02:00``) must be included/excluded by its real instant, not by raw
        ISO-string comparison. Window center 2026-05-20 ±1d → [2026-05-19T00Z,
        2026-05-22T00Z] (the window includes the whole of its last day). The
        +02:00 chunk lands at 2026-05-20T23:00Z (inside),
        even though its raw string ``…T01:00:00+02:00`` sorts AFTER the window's
        upper bound ``…T00:00:00+00:00`` — the lexicographic bug would drop it.
        """
        # +02:00 → 2026-05-20T23:00:00Z, inside the window by real instant.
        await ingest_chunk(
            text="evening event (Paris time)",
            source="calendar",
            content_hash="tz-in",
            source_id="tz-1",
            date="2026-05-21T01:00:00+02:00",
        )
        # UTC chunk near the same real instant, also inside.
        await ingest_chunk(
            text="late ping",
            source="whatsapp",
            content_hash="tz-utc",
            source_id="tz-2",
            date="2026-05-20T22:30:00Z",
        )
        # +02:00 → 2026-05-22T10:00:00Z, genuinely outside the window.
        await ingest_chunk(
            text="next-day event (Paris time)",
            source="calendar",
            content_hash="tz-out",
            source_id="tz-3",
            date="2026-05-22T12:00:00+02:00",
        )
        out = await fetch_around("2026-05-20", window_days=1)
        keys = {c["source_id"] for c in out}
        assert "tz-1" in keys  # offset chunk inside by real instant
        assert "tz-2" in keys  # UTC chunk inside
        assert "tz-3" not in keys  # offset chunk outside by real instant

    @pytest.mark.regression
    async def test_order_by_real_instant_across_offsets(self, db, mock_qdrant):
        """newest-first ordering must compare real instants, not raw ISO text.

        Two chunks in the same window with offsets that make lexical and
        instant order diverge:

          * ``2026-05-21T01:00:00+02:00`` → 2026-05-20T23:00Z (older instant),
            yet its raw string sorts AFTER the UTC one below;
          * ``2026-05-20T23:30:00+00:00`` → 23:30Z (newer instant), raw string
            sorts BEFORE.

        Under the old ``ORDER BY date_ts`` (lexical) the ``+02:00`` chunk would
        come first; ``ORDER BY datetime(date_ts)`` puts the genuinely-newer
        23:30Z chunk first.
        """
        await ingest_chunk(
            text="Paris-time event 01:00+02:00 (= 23:00Z)",
            source="calendar",
            content_hash="ord-paris",
            source_id="ord-paris",
            date="2026-05-21T01:00:00+02:00",
        )
        await ingest_chunk(
            text="UTC ping 23:30Z (newer instant)",
            source="whatsapp",
            content_hash="ord-utc",
            source_id="ord-utc",
            date="2026-05-20T23:30:00+00:00",
        )
        out = await fetch_around("2026-05-20", window_days=1)
        order = [c["source_id"] for c in out]
        assert order.index("ord-utc") < order.index("ord-paris")

    async def test_structured_calendar_fields_round_trip(self, db, mock_qdrant):
        """event_type / event_status / working_location persist through ingest
        and come back as fields on the fetch_around result — the path the
        Briefing relies on instead of parsing the chunk text."""
        await ingest_chunk(
            text="Sprint review\n2026-05-20T10:00 → 2026-05-20T11:00\nParis HQ",
            source="gcal",
            content_hash="gcal-struct",
            source_id="evt-struct",
            date="2026-05-20T10:00:00Z",
            event_type="outOfOffice",
            event_status="tentative",
            working_location="Télétravail (home office)",
        )
        out = await fetch_around("2026-05-20", window_days=1, sources=["gcal"])
        assert len(out) == 1
        assert out[0]["event_type"] == "outOfOffice"
        assert out[0]["event_status"] == "tentative"
        assert out[0]["working_location"] == "Télétravail (home office)"

    async def test_blank_structured_fields_stored_as_null(self, db, mock_qdrant):
        """A connector sending "" for "no working location" collapses to NULL,
        so the field reads as absent rather than an empty string."""
        await ingest_chunk(
            text="Standup",
            source="gcal",
            content_hash="gcal-plain",
            source_id="evt-plain",
            date="2026-05-20T09:00:00Z",
            event_type="default",
            event_status="confirmed",
            working_location="",
        )
        out = await fetch_around("2026-05-20", window_days=1, sources=["gcal"])
        assert out[0]["working_location"] is None
        assert out[0]["event_type"] == "default"
