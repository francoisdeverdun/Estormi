"""Behaviour tests for the iMessage ingestion fetcher.

Covers the pure helpers (``apple_ts_to_dt``, ``decode_attributed_body``,
``_safe_id``), the snapshot/loopback boundary (``_request_snapshot``,
``_resolve_chat_db``), and the end-to-end ``fetch`` path against a real
temporary chat.db built with the minimal schema the query touches.

Every external boundary is mocked: no loopback HTTP call ever fires and no
real ~/Library/Messages/chat.db is read.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

import pytest

from estormi_ingestion.imessage import fetch_imessages as fim

pytestmark = pytest.mark.integration


# ── blob helper ─────────────────────────────────────────────────────────────


def _make_attributed_body(text: str) -> bytes:
    """Craft a minimal Messages ``attributedBody`` typedstream blob.

    Mirrors the layout the decoder walks: an ``NSString`` class marker, a
    ``+`` (0x2b) within the 16-byte scan window, then a variable-width
    length prefix followed by the UTF-8 run.
    """
    b = text.encode("utf-8")
    n = len(b)
    if n < 0x81:
        prefix = bytes([n])
    elif n <= 0xFFFF:
        prefix = bytes([0x81]) + n.to_bytes(2, "little")
    elif n <= 0xFFFF_FFFF:
        prefix = bytes([0x82]) + n.to_bytes(4, "little")
    else:
        prefix = bytes([0x83]) + n.to_bytes(8, "little")
    return b"\x00\x01NSString\x01\x95\x84\x01\x2b" + prefix + b


# ── apple_ts_to_dt ──────────────────────────────────────────────────────────


class TestAppleTsToDt:
    def test_zero_anchors_to_apple_epoch(self):
        dt = fim.apple_ts_to_dt(0)
        assert dt == fim.APPLE_EPOCH

    def test_nanosecond_branch(self):
        # > 1e10 routes through the nanosecond divisor.
        ns = 738_000_000_000_000_000
        dt = fim.apple_ts_to_dt(ns)
        assert dt.year >= 2024
        assert dt.tzinfo is timezone.utc

    def test_second_branch(self):
        secs = 738_000_000
        dt = fim.apple_ts_to_dt(secs)
        assert dt.year >= 2024
        assert dt.tzinfo is timezone.utc

    def test_one_second_after_epoch(self):
        dt = fim.apple_ts_to_dt(1)
        assert (dt - fim.APPLE_EPOCH).total_seconds() == 1

    def test_overflow_falls_back_to_epoch(self):
        # An absurd nanosecond value overflows fromtimestamp -> sentinel epoch.
        dt = fim.apple_ts_to_dt(10**30)
        assert dt == fim.APPLE_EPOCH


# ── decode_attributed_body ──────────────────────────────────────────────────


class TestDecodeAttributedBody:
    def test_none_blob(self):
        assert fim.decode_attributed_body(None) == ""

    def test_empty_blob(self):
        assert fim.decode_attributed_body(b"") == ""

    def test_no_nsstring_marker(self):
        assert fim.decode_attributed_body(b"garbage with no marker") == ""

    def test_marker_without_plus(self):
        # Marker present but no 0x2b within the scan window.
        assert fim.decode_attributed_body(b"\x00NSString\x00\x00\x00") == ""

    def test_short_length_prefix(self):
        assert fim.decode_attributed_body(_make_attributed_body("Hello world")) == "Hello world"

    def test_two_byte_length_prefix(self):
        text = "x" * 200  # > 0x80 forces the 0x81 (2-byte) path
        assert fim.decode_attributed_body(_make_attributed_body(text)) == text

    def test_four_byte_length_prefix(self):
        text = "Hi"
        blob = b"\x00NSString\x2b\x82" + (len(text)).to_bytes(4, "little") + text.encode()
        assert fim.decode_attributed_body(blob) == text

    def test_declared_length_overruns_blob(self):
        # A 0x81 prefix declaring far more bytes than present -> guard returns "".
        blob = b"\x00NSString\x2b\x81" + (9999).to_bytes(2, "little") + b"short"
        assert fim.decode_attributed_body(blob) == ""

    def test_invalid_utf8_is_replaced(self):
        # One declared byte of invalid UTF-8 decodes to the replacement char.
        blob = b"\x00NSString\x2b\x01\xff"
        assert fim.decode_attributed_body(blob) == "�"


# ── _safe_id ────────────────────────────────────────────────────────────────


class TestSafeId:
    def test_deterministic_and_truncated(self):
        out = fim._safe_id("ABC-GUID-123")
        assert len(out) == 32
        assert out == fim._safe_id("ABC-GUID-123")

    def test_distinct_inputs_distinct_ids(self):
        assert fim._safe_id("a") != fim._safe_id("b")

    def test_matches_sha256_prefix(self):
        import hashlib

        guid = "some-guid"
        assert fim._safe_id(guid) == hashlib.sha256(guid.encode()).hexdigest()[:32]


# ── _request_snapshot ───────────────────────────────────────────────────────


class TestRequestSnapshot:
    def test_no_token_returns_false_without_http(self, monkeypatch):
        monkeypatch.delenv("ESTORMI_WA_TOKEN", raising=False)

        def _boom(*a, **k):  # pragma: no cover - must never be called
            raise AssertionError("urlopen must not be called without a token")

        monkeypatch.setattr("urllib.request.urlopen", _boom)
        assert fim._request_snapshot() is False

    def test_authorized_response(self, monkeypatch):
        monkeypatch.setenv("ESTORMI_WA_TOKEN", "tok")

        class _Resp:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return json.dumps({"status": "authorized"}).encode()

        monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k: _Resp())
        assert fim._request_snapshot() is True

    def test_unauthorized_status(self, monkeypatch):
        monkeypatch.setenv("ESTORMI_WA_TOKEN", "tok")

        class _Resp:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return json.dumps({"status": "denied"}).encode()

        monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k: _Resp())
        assert fim._request_snapshot() is False

    def test_host_unreachable_returns_false(self, monkeypatch):
        monkeypatch.setenv("ESTORMI_WA_TOKEN", "tok")

        def _raise(*a, **k):
            raise OSError("connection refused")

        monkeypatch.setattr("urllib.request.urlopen", _raise)
        assert fim._request_snapshot() is False


# ── _resolve_chat_db ────────────────────────────────────────────────────────


class TestResolveChatDb:
    def test_explicit_override_wins(self, monkeypatch, tmp_path):
        override = tmp_path / "explicit.db"
        monkeypatch.setenv("IMESSAGE_DB", str(override))

        def _boom():  # pragma: no cover - override short-circuits before snapshot
            raise AssertionError("snapshot must not run when override is set")

        monkeypatch.setattr(fim, "_request_snapshot", _boom)
        assert fim._resolve_chat_db() == override

    def test_snapshot_copy_preferred_when_present(self, monkeypatch, tmp_path):
        monkeypatch.delenv("IMESSAGE_DB", raising=False)
        monkeypatch.setattr(fim, "_request_snapshot", lambda: True)
        data_dir = tmp_path / "data"
        copy = data_dir / "imessage" / "chat.db"
        copy.parent.mkdir(parents=True)
        copy.write_bytes(b"")
        monkeypatch.setattr(fim, "estormi_data_dir", lambda: data_dir)
        assert fim._resolve_chat_db() == copy

    def test_falls_back_to_live_original(self, monkeypatch, tmp_path):
        monkeypatch.delenv("IMESSAGE_DB", raising=False)
        monkeypatch.setattr(fim, "_request_snapshot", lambda: False)
        monkeypatch.setattr(fim, "estormi_data_dir", lambda: tmp_path / "empty")
        resolved = fim._resolve_chat_db()
        assert resolved.match("Library/Messages/chat.db")


# ── fetch ───────────────────────────────────────────────────────────────────


def _build_chat_db(path) -> None:
    """Create a chat.db with the minimal schema fetch() queries."""
    con = sqlite3.connect(path)
    try:
        con.executescript(
            """
            CREATE TABLE message (
                rowid INTEGER PRIMARY KEY,
                guid TEXT,
                text TEXT,
                attributedBody BLOB,
                is_from_me INTEGER,
                date INTEGER,
                service TEXT,
                handle_id INTEGER
            );
            CREATE TABLE handle (rowid INTEGER PRIMARY KEY, id TEXT);
            CREATE TABLE chat (
                rowid INTEGER PRIMARY KEY,
                display_name TEXT,
                chat_identifier TEXT
            );
            CREATE TABLE chat_message_join (chat_id INTEGER, message_id INTEGER);
            """
        )
        con.commit()
    finally:
        con.close()


def _now_ns() -> int:
    """A current message timestamp in Apple-epoch nanoseconds."""
    return int(
        (datetime.now(tz=timezone.utc).timestamp() - fim.APPLE_EPOCH.timestamp()) * 1_000_000_000
    )


def _seed_message(path, **cols) -> None:
    con = sqlite3.connect(path)
    try:
        defaults = {
            "guid": "G-1",
            "text": None,
            "attributedBody": None,
            "is_from_me": 0,
            "date": _now_ns(),
            "service": "iMessage",
            "handle_id": None,
        }
        defaults.update(cols)
        con.execute(
            "INSERT INTO message (guid, text, attributedBody, is_from_me, date, "
            "service, handle_id) VALUES (:guid, :text, :attributedBody, "
            ":is_from_me, :date, :service, :handle_id)",
            defaults,
        )
        con.commit()
    finally:
        con.close()


def _add_handle(path, rowid, ident) -> None:
    con = sqlite3.connect(path)
    try:
        con.execute("INSERT INTO handle (rowid, id) VALUES (?, ?)", (rowid, ident))
        con.commit()
    finally:
        con.close()


@pytest.fixture
def chat_db(tmp_path, monkeypatch):
    """A temp chat.db wired up as the resolved DB, with STAGING redirected."""
    db = tmp_path / "chat.db"
    _build_chat_db(db)
    staging = tmp_path / "staging"
    monkeypatch.setattr(fim, "_resolve_chat_db", lambda: db)
    monkeypatch.setattr(fim, "_request_snapshot", lambda: False)
    monkeypatch.setattr(fim, "STAGING", staging)
    return db, staging


class TestFetch:
    def test_missing_db_exits_1(self, tmp_path, monkeypatch):
        missing = tmp_path / "nope.db"
        monkeypatch.setattr(fim, "_resolve_chat_db", lambda: missing)
        with pytest.raises(SystemExit) as exc:
            fim.fetch(days=30)
        assert exc.value.code == 1

    def test_empty_results_returns_zero(self, chat_db):
        _db, staging = chat_db
        assert fim.fetch(days=30) == 0
        # Nothing staged when there are no rows.
        assert not staging.exists()

    def test_plain_text_message_is_staged(self, chat_db):
        db, staging = chat_db
        _add_handle(db, 1, "+15550001111")
        _seed_message(db, guid="G-plain", text="Hello there", handle_id=1)

        count = fim.fetch(days=30)
        assert count == 1

        safe = fim._safe_id("G-plain")
        body = staging / f"{safe}.txt"
        meta = staging / f"{safe}.meta.json"
        assert body.read_text(encoding="utf-8") == "Hello there"
        payload = json.loads(meta.read_text(encoding="utf-8"))
        assert payload["id"] == "G-plain"
        assert payload["from"] == "+15550001111"
        assert payload["name"] == "+15550001111"
        assert payload["service"] == "iMessage"
        # Max-date watermark written for non-dry runs with rows.
        assert (staging / "_max_date.txt").read_text().strip()

    def test_attributed_body_decoded_when_text_null(self, chat_db):
        db, staging = chat_db
        _seed_message(
            db,
            guid="G-attr",
            text=None,
            attributedBody=_make_attributed_body("From the blob"),
        )
        assert fim.fetch(days=30) == 1
        safe = fim._safe_id("G-attr")
        assert (staging / f"{safe}.txt").read_text(encoding="utf-8") == "From the blob"

    def test_is_from_me_names_me(self, chat_db):
        db, staging = chat_db
        _seed_message(db, guid="G-me", text="mine", is_from_me=1)
        assert fim.fetch(days=30) == 1
        meta = json.loads((staging / f"{fim._safe_id('G-me')}.meta.json").read_text())
        assert meta["name"] == "Me"

    def test_dry_run_stages_nothing(self, chat_db):
        db, staging = chat_db
        _seed_message(db, guid="G-dry", text="not persisted")
        assert fim.fetch(days=30, dry_run=True) == 0
        assert not staging.exists()

    def test_attachment_only_message_skipped(self, chat_db):
        db, staging = chat_db
        # Object-replacement char only -> stripped to empty -> skipped.
        _seed_message(db, guid="G-att", text="￼")
        assert fim.fetch(days=30) == 0

    def test_text_with_attachment_marker_kept(self, chat_db):
        db, staging = chat_db
        _seed_message(db, guid="G-mix", text="Look ￼ here")
        assert fim.fetch(days=30) == 1
        body = (staging / f"{fim._safe_id('G-mix')}.txt").read_text(encoding="utf-8")
        assert body == "Look  here"

    def test_otp_message_skipped(self, chat_db):
        db, staging = chat_db
        _seed_message(db, guid="G-otp", text="Your verification code is 123456")
        assert fim.fetch(days=30) == 0

    def test_older_than_window_excluded(self, chat_db):
        db, staging = chat_db
        # A genuinely old message (Apple epoch + 1 day, a 2001 seconds-valued
        # timestamp) is well before the 30-day cutoff and filtered in SQL.
        _seed_message(db, guid="G-old", text="ancient", date=86_400)
        assert fim.fetch(days=30) == 0

    def test_recent_legacy_seconds_row_included(self, chat_db):
        # Regression: legacy SMS/old rows store `message.date` in SECONDS
        # (~7e8), modern rows in nanoseconds (~7e17). The cutoff used to be a
        # nanosecond boundary, which is always greater than any seconds value,
        # so a RECENT legacy message was silently excluded forever. The cutoff
        # now normalises units, so a recent seconds-valued row is ingested.
        db, staging = chat_db
        now_seconds = int(datetime.now(tz=timezone.utc).timestamp() - fim.APPLE_EPOCH.timestamp())
        assert now_seconds < fim.APPLE_NS_THRESHOLD  # sanity: this is a seconds value
        _seed_message(db, guid="G-legacy", text="recent legacy sms", date=now_seconds)
        assert fim.fetch(days=30) == 1

    def test_empty_guid_hashes_fallback_identity(self, chat_db):
        db, staging = chat_db
        _add_handle(db, 1, "alice")
        ts = _now_ns()
        _seed_message(db, guid="", text="no guid", handle_id=1, date=ts)
        assert fim.fetch(days=30) == 1
        expected = fim._safe_id(f"alice:{ts}:no guid")
        assert (staging / f"{expected}.txt").exists()

    def test_corrupt_db_exits_0(self, tmp_path, monkeypatch):
        # A file that is not a valid SQLite DB triggers the DatabaseError branch.
        bad = tmp_path / "corrupt.db"
        bad.write_bytes(b"this is not sqlite")
        monkeypatch.setattr(fim, "_resolve_chat_db", lambda: bad)
        with pytest.raises(SystemExit) as exc:
            fim.fetch(days=30)
        assert exc.value.code == 0

    def test_failed_decode_counted_but_run_succeeds(self, chat_db, capsys):
        db, staging = chat_db
        # NULL text + a non-empty but undecodable blob -> failed_decode increment.
        _seed_message(db, guid="G-bad", text=None, attributedBody=b"no marker here")
        assert fim.fetch(days=30) == 0
        err = capsys.readouterr().err
        assert "decoded to empty" in err
