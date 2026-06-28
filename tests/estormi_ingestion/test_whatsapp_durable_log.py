"""Tests for the durable WhatsApp message log + timestamp watermark.

The log is the local source of truth: staged messages are appended (idempotent
by msg_id), then `whatsapp` chunks are derived from it by a timestamp watermark,
so re-ingestion never needs to re-contact WhatsApp. These tests cover the log
helpers in isolation against a temp DB.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from estormi_ingestion.whatsapp import ingest_conversations as ic

pytestmark = pytest.mark.unit


def _make_db(path: str) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE whatsapp_messages (
            msg_id TEXT PRIMARY KEY, chat_id TEXT NOT NULL, chat_name TEXT,
            sender_name TEXT, ts_iso TEXT NOT NULL, text TEXT NOT NULL,
            archived_at TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE ingestion_watermarks (
            source TEXT PRIMARY KEY, last_fetched_at TEXT, last_item_id TEXT
        );
        """
    )
    conn.commit()
    conn.close()


@pytest.fixture()
def log_db(tmp_path, monkeypatch):
    db = tmp_path / "log.db"
    _make_db(str(db))
    monkeypatch.setenv("ESTORMI_DB", str(db))
    return str(db)


def _msg(mid, chat, ts, text="hello there friend, a long enough message"):
    return {
        "msg_id": mid,
        "chat_id": chat,
        "chat_name": "Chat",
        "name": "Alice",
        "timestamp_iso": ts,
        "text": text,
        "meta_file": None,
        "body_file": None,
    }


def test_append_is_idempotent(log_db):
    msgs = [
        _msg("m1", "c@g.us", "2026-06-01T10:00:00+00:00"),
        _msg("m2", "c@g.us", "2026-06-01T10:05:00+00:00"),
    ]
    assert ic.append_to_log(msgs) == 2
    assert ic.append_to_log(msgs) == 0  # same ids → nothing new
    assert ic.append_to_log([_msg("m3", "c@g.us", "2026-06-01T10:10:00+00:00")]) == 1


def test_load_log_since_filters_and_orders(log_db):
    ic.append_to_log(
        [
            _msg("a", "c", "2026-06-01T08:00:00+00:00"),
            _msg("b", "c", "2026-06-02T08:00:00+00:00"),
            _msg("c", "c", "2026-06-03T08:00:00+00:00"),
        ]
    )
    assert [m["msg_id"] for m in ic.load_log_since("2026-06-02T00:00:00+00:00")] == ["b", "c"]
    # Empty cutoff = everything, ordered by ts.
    assert [m["msg_id"] for m in ic.load_log_since("")] == ["a", "b", "c"]


def test_load_log_since_shapes_like_staged(log_db):
    ic.append_to_log([_msg("a", "chat@g.us", "2026-06-01T08:00:00+00:00", "the body")])
    (m,) = ic.load_log_since("")
    assert m["chat_id"] == "chat@g.us"
    assert m["name"] == "Alice"
    assert m["text"] == "the body"
    assert m["meta_file"] is None and m["body_file"] is None


def test_watermark_roundtrip(log_db):
    assert ic.get_log_watermark() is None
    ic.set_log_watermark("2026-06-02T12:00:00+00:00")
    assert ic.get_log_watermark() == "2026-06-02T12:00:00+00:00"
    ic.set_log_watermark("2026-06-03T12:00:00+00:00")  # upsert
    assert ic.get_log_watermark() == "2026-06-03T12:00:00+00:00"


def test_window_cutoff_applies_overlap():
    wm = "2026-06-02T12:00:00+00:00"
    expected = (datetime.fromisoformat(wm) - timedelta(seconds=ic.WINDOW_GAP * 2)).isoformat()
    assert ic._window_cutoff(wm) == expected
    assert ic._window_cutoff(None) == ""
    assert ic._window_cutoff("not-a-date") == ""


def test_prune_drops_old_keeps_recent(log_db):
    now = datetime.now(timezone.utc)
    ic.append_to_log(
        [
            _msg("old", "c", (now - timedelta(days=200)).isoformat()),
            _msg("new", "c", (now - timedelta(days=1)).isoformat()),
        ]
    )
    assert ic.prune_log(retention_days=90) == 1
    assert [m["msg_id"] for m in ic.load_log_since("")] == ["new"]


def test_append_content_hash_fallback_when_no_msg_id(log_db):
    """Messages with no WhatsApp id still dedup by content hash."""
    m1 = _msg("", "c", "2026-06-01T10:00:00+00:00", "first message text, plenty long")
    m2 = _msg("", "c", "2026-06-01T10:01:00+00:00", "second message text, plenty long")
    assert ic.append_to_log([m1, m2]) == 2
    assert ic.append_to_log([m1]) == 0  # identical content → same hash → ignored


def test_append_normalises_timestamps_to_canonical_utc(log_db):
    """The log's ordering contract: every stored ts_iso is canonical UTC, so
    the SQL range scan and the lexical watermark max() order by real instant.
    A 'Z' suffix and a non-UTC offset must both normalise at append."""
    ic.append_to_log(
        [
            _msg("z", "c", "2026-06-01T10:00:00Z"),
            _msg("o", "c", "2026-06-01T13:00:00+02:00"),  # = 11:00 UTC
        ]
    )
    assert [m["timestamp_iso"] for m in ic.load_log_since("")] == [
        "2026-06-01T10:00:00+00:00",
        "2026-06-01T11:00:00+00:00",
    ]


def test_extend_to_window_starts_recovers_conversation_head(log_db):
    """A slice cut mid-conversation must be pulled back to the conversation's
    true first message — but not across a real (> WINDOW_GAP) silence."""
    t0 = datetime(2026, 6, 1, 10, 0, tzinfo=timezone.utc)
    step = ic.WINDOW_GAP // 2  # well within the same conversation window
    # An older, separate conversation (> WINDOW_GAP before t0) — must stay out.
    ic.append_to_log(
        [_msg("m0", "c@g.us", (t0 - timedelta(seconds=ic.WINDOW_GAP * 2)).isoformat())]
    )
    ic.append_to_log(
        [
            _msg(f"m{i}", "c@g.us", (t0 + timedelta(seconds=(i - 1) * step)).isoformat())
            for i in range(1, 5)
        ]
    )
    # Slice from m3 onwards — the cutoff falls in the middle of the conversation.
    sliced = ic.load_log_since((t0 + timedelta(seconds=2 * step)).isoformat())
    assert [m["msg_id"] for m in sliced] == ["m3", "m4"]

    extended = ic._extend_to_window_starts(sliced)
    assert sorted(m["msg_id"] for m in extended) == ["m1", "m2", "m3", "m4"]
    assert ic._extend_to_window_starts([]) == []


class TestWindowGrowth:
    """Bug U4: a WhatsApp window that grows across runs must not silently drop
    the newly-appended messages.

    A window grows when new messages arrive within ``WINDOW_GAP`` of the last
    one: ``group_into_windows`` folds them into the same window with an
    *unchanged* ``first_ts``. The pre-fix ``ingest_window`` keyed idx0's
    ``base_hash`` only on ``(chat_id, first_ts)``, so its ``content_hash`` was
    identical run-to-run; ``ingest_chunk`` dedups on exact ``content_hash``
    *before* the ``source_id`` stale-replace path runs, so the grown window's
    first sub-window froze at its first-run contents. The fix derives
    ``base_hash`` from the current message set (``window_id`` + last message
    timestamp + message count)."""

    _CHAT_ID = "33600000000@s.whatsapp.net"
    _T0 = "2026-06-01T10:00:00+00:00"

    @classmethod
    def _msg(cls, mid: str, ts: str, text: str) -> dict:
        return {
            "msg_id": mid,
            "chat_id": cls._CHAT_ID,
            "chat_name": "Alice",
            "name": "Alice",
            "timestamp_iso": ts,
            "text": text,
            "meta_file": None,
            "body_file": None,
        }

    @classmethod
    def _w1(cls) -> list[dict]:
        return [
            cls._msg("m1", cls._T0, "hey there, how did the demo go this afternoon"),
            cls._msg("m2", "2026-06-01T10:01:00+00:00", "it went well, the client signed off"),
            cls._msg("m3", "2026-06-01T10:02:00+00:00", "amazing news, congratulations team"),
        ]

    @classmethod
    def _w2(cls) -> list[dict]:
        # Same window grown by two messages within WINDOW_GAP: first_ts stays T0
        # but the window now carries five messages.
        return cls._w1() + [
            cls._msg("m4", "2026-06-01T10:03:00+00:00", "we should celebrate this milestone soon"),
            cls._msg("m5", "2026-06-01T10:04:00+00:00", "drinks on friday then, i'll book a table"),
        ]

    @classmethod
    def _idx0_hash(cls, window: list[dict]) -> str:
        """Reproduce ingest_window's content_hash for idx0 (the fixed scheme)."""
        import hashlib

        first_ts = window[0]["timestamp_iso"]
        window_id = f"{cls._CHAT_ID}:{first_ts}"
        last_ts = window[-1]["timestamp_iso"]
        base_hash = hashlib.sha256(f"{window_id}:{last_ts}:{len(window)}".encode()).hexdigest()
        return f"{base_hash}-0"

    @classmethod
    def _old_idx0_hash(cls, window: list[dict]) -> str:
        """The pre-fix derivation: base_hash keyed only on (chat_id, first_ts)."""
        import hashlib

        first_ts = window[0]["timestamp_iso"]
        window_id = f"{cls._CHAT_ID}:{first_ts}"
        base_hash = hashlib.sha256(window_id.encode()).hexdigest()
        return f"{base_hash}-0"

    def test_grown_window_changes_idx0_content_hash(self):
        """A grown window (same first_ts, more messages) must produce a DIFFERENT
        idx0 content_hash so ingest_chunk's duplicate short-circuit won't fire."""
        assert self._idx0_hash(self._w1()) != self._idx0_hash(self._w2())

    def test_old_scheme_would_freeze_idx0_hash(self):
        """Red proof: the pre-fix scheme produced IDENTICAL hashes for W1 and W2
        — the exact mechanism behind bug U4. The fix must diverge from it."""
        assert self._old_idx0_hash(self._w1()) == self._old_idx0_hash(self._w2())
        assert self._idx0_hash(self._w2()) != self._old_idx0_hash(self._w2())


def test_ingest_window_marks_payload_pii_filtered(monkeypatch):
    """Every WhatsApp chunk must carry meta {"pii_filtered": True}.

    The window body is already scrubbed per message by _format_sub_window
    (filter_pii + OTP-only line drop). Without the flag, the server's
    whole-chunk OTP heuristic (estormi_server/storage/writers.py) silently DROPS any
    chunk whose text contains an OTP-shaped phrase like "expires in 3" or
    "don't share this" — counting it as success while the watermark advances,
    so ordinary messages are lost forever. The flag tells the server the chunk
    was pre-scrubbed and must not be dropped wholesale.
    """
    posted: list[dict] = []

    class _Resp:
        status_code = 200
        text = "{}"

        def json(self) -> dict:
            return {}

        def raise_for_status(self) -> None:
            return None

    def _fake_batch(url, chunks, timeout=120, retries=6, backoff=1.0, headers=None):
        posted.extend(chunks)
        return _Resp()

    monkeypatch.setattr(ic, "post_batch", _fake_batch)
    monkeypatch.setattr(ic, "_resolved_chat_name", lambda _cid: "")

    chat_id = "33600000000@s.whatsapp.net"
    window = [
        _msg("w1", chat_id, "2026-06-01T10:00:00+00:00", "the early bird deal expires in 3 days"),
        _msg("w2", chat_id, "2026-06-01T10:01:00+00:00", "ok lets book the flights this evening"),
    ]

    assert ic.ingest_window(chat_id, "Alice", window) == ic.OUTCOME_OK
    assert posted, "expected at least one chunk to be POSTed"
    for payload in posted:
        assert payload.get("meta") == {"pii_filtered": True}

    def test_ingest_window_posts_fresh_hash_for_grown_window(self, monkeypatch):
        """End-to-end through ingest_window: capture the POSTed payloads and
        assert (1) the grown window's idx0 content_hash differs from the first
        run's, and (2) the grown window's chunk text reflects the new messages."""
        posted: list[dict] = []

        class _Resp:
            status_code = 200
            text = "{}"

            def json(self) -> dict:
                return {}

            def raise_for_status(self) -> None:
                return None

        def _fake_batch(url, chunks, timeout=120, retries=6, backoff=1.0, headers=None):
            posted.extend(chunks)
            return _Resp()

        monkeypatch.setattr(ic, "post_batch", _fake_batch)
        monkeypatch.setattr(ic, "_resolved_chat_name", lambda _cid: "")

        assert ic.ingest_window(self._CHAT_ID, "Alice", self._w1()) == ic.OUTCOME_OK
        first_run = list(posted)
        posted.clear()
        assert ic.ingest_window(self._CHAT_ID, "Alice", self._w2()) == ic.OUTCOME_OK
        grown_run = list(posted)

        assert first_run, "first run should POST at least one chunk"
        assert grown_run, "grown run should POST at least one chunk"

        # source_id stays the window_id so stale-replace targets this conversation.
        window_id = f"{self._CHAT_ID}:{self._T0}"
        assert all(p["source_id"] == window_id for p in first_run + grown_run)

        # idx0's content_hash must change between runs (duplicate skip bypassed).
        first_idx0 = next(p for p in first_run if p["content_hash"].endswith("-0"))
        grown_idx0 = next(p for p in grown_run if p["content_hash"].endswith("-0"))
        assert first_idx0["content_hash"] != grown_idx0["content_hash"]

        # The grown window's text must reflect all five messages, including m4/m5.
        grown_text = "\n".join(p["text"] for p in grown_run)
        assert "celebrate this milestone" in grown_text
        assert "drinks on friday" in grown_text


class TestStraddleRewindow:
    """The replay cutoff is global (watermark − 2×WINDOW_GAP across ALL chats),
    so it can fall in the MIDDLE of a conversation — one longer than the
    overlap, or one in a chat that went quiet while another stayed active.

    Pre-fix, the re-derived window then started at its first message ≥ cutoff,
    yielding a new ``window_id`` (= ``source_id``) and a fresh content_hash:
    the stale-replace path never retired the run-N chunks and the conversation
    tail was re-ingested as a permanent duplicate. ``_extend_to_window_starts``
    walks each chat back to its true conversation start so ``window_id`` stays
    stable across runs."""

    _CHAT_ID = "33611223344@s.whatsapp.net"
    _T0 = datetime(2026, 6, 1, 10, 0, tzinfo=timezone.utc)
    # Messages spaced at 2/3 of a gap stay in one conversation window while the
    # total span (4 steps) exceeds the 2×WINDOW_GAP replay overlap.
    _STEP = ic.WINDOW_GAP * 2 // 3

    @classmethod
    def _msgs(cls) -> list[dict]:
        texts = [
            "hey, are we still on for the climbing session this weekend",
            "yes definitely, i booked the wall for saturday morning",
            "perfect, i'll bring the rope and the quickdraws",
            "great, let's meet at the entrance around nine thirty",
            "works for me, see you there — don't forget the chalk",
        ]
        return [
            _msg(f"s{i}", cls._CHAT_ID, (cls._T0 + timedelta(seconds=i * cls._STEP)).isoformat(), t)
            for i, t in enumerate(texts)
        ]

    @staticmethod
    def _derive_and_ingest() -> None:
        """Mirror main()'s derive phase: cutoff → slice → extend → ingest →
        advance watermark."""
        cutoff = ic._window_cutoff(ic.get_log_watermark())
        log_msgs = ic._extend_to_window_starts(ic.load_log_since(cutoff))
        for chat_id, chat_name, window in ic.group_into_windows(log_msgs):
            assert ic.ingest_window(chat_id, chat_name, window) == ic.OUTCOME_OK
        ic.set_log_watermark(max(m["timestamp_iso"] for m in log_msgs))

    def test_rewindow_across_straddling_cutoff_creates_no_new_source_id(self, log_db, monkeypatch):
        posted: list[dict] = []

        class _Resp:
            status_code = 200
            text = "{}"

            def json(self) -> dict:
                return {}

            def raise_for_status(self) -> None:
                return None

        def _fake_batch(url, chunks, timeout=120, retries=6, backoff=1.0, headers=None):
            posted.extend(chunks)
            return _Resp()

        monkeypatch.setattr(ic, "post_batch", _fake_batch)
        monkeypatch.setattr(ic, "_resolved_chat_name", lambda _cid: "")

        msgs = self._msgs()
        assert ic.append_to_log(msgs) == len(msgs)

        # Run N: first ingestion of the whole conversation.
        self._derive_and_ingest()
        run1 = list(posted)
        posted.clear()
        assert run1, "run N should POST at least one chunk"

        # Sanity: run N+1's cutoff falls strictly INSIDE the conversation —
        # the straddle this test is about.
        cutoff = ic._window_cutoff(ic.get_log_watermark())
        assert msgs[0]["timestamp_iso"] < cutoff <= msgs[-1]["timestamp_iso"]

        # Run N+1: nothing new arrived; the overlap slice is re-windowed.
        self._derive_and_ingest()
        run2 = list(posted)
        assert run2, "run N+1 re-posts the reconstructed window"

        # NO new source_id may appear — the window must re-derive identically.
        assert {p["source_id"] for p in run2} == {p["source_id"] for p in run1}
        assert all(p["source_id"] == f"{self._CHAT_ID}:{msgs[0]['timestamp_iso']}" for p in run2)
        # And every re-posted chunk carries an already-seen content_hash, so the
        # server's duplicate short-circuit skips it — zero duplicated chunks.
        assert {p["content_hash"] for p in run2} <= {p["content_hash"] for p in run1}
