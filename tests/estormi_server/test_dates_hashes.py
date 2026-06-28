"""Tests for date conversion helpers and content-hash semantics."""

from __future__ import annotations

import hashlib

import pytest

# ── apple_ts_to_dt (iMessage) ──────────────────────────────────────────────
from estormi_ingestion.imessage.fetch_imessages import apple_ts_to_dt
from estormi_server.storage.search_api import _parse_date_ts

pytestmark = pytest.mark.unit


class TestAppleTsToDt:
    def test_nanosecond_timestamp(self):
        # ~2024 in nanoseconds since Apple epoch
        ns = 738_000_000_000_000_000  # ~2024-05
        dt = apple_ts_to_dt(ns)
        assert dt.year >= 2024
        assert dt.tzinfo is not None

    def test_second_timestamp(self):
        # ~2024 in seconds since Apple epoch
        secs = 738_000_000
        dt = apple_ts_to_dt(secs)
        assert dt.year >= 2024
        assert dt.tzinfo is not None

    def test_zero_returns_apple_epoch(self):
        # A row with ts==0 means "no timestamp" — return the deterministic Apple
        # epoch so downstream code can either filter or render it predictably.
        # Returning datetime.now() would silently mis-stamp old/corrupt rows as
        # "today" and sweep them into the current ingest window.
        dt = apple_ts_to_dt(0)
        assert dt.year == 2001
        assert dt.month == 1
        assert dt.day == 1

    def test_known_value(self):
        # Apple epoch + 0 seconds = 2001-01-01 UTC
        dt = apple_ts_to_dt(1)  # 1 second after epoch
        assert dt.year == 2001
        assert dt.month == 1
        assert dt.day == 1

    def test_negative_offsets_the_apple_epoch(self):
        # -1 second is a representable offset from the Apple epoch, so it
        # is computed normally (not routed through the OSError sentinel
        # branch) and lands one second before 2001-01-01 UTC.
        dt = apple_ts_to_dt(-1)
        assert dt is not None
        assert dt.year == 2000
        assert dt.month == 12
        assert dt.day == 31
        assert dt.hour == 23
        assert dt.minute == 59
        assert dt.second == 59


# ── Content hash semantics ──────────────────────────────────────────────────


class TestContentHashSemantics:
    """Tests documenting the expected content_hash conventions per source."""

    def test_full_text_hash_folds_source_id(self):
        """notes, mail, documents, code, calendar fold source_id into the base:
        sha256(f"{source_id}|{full_text}"). The id is REQUIRED because the server
        dedups GLOBALLY on content_hash, so two distinct files/sources with
        byte-identical text must still produce different hashes (or the second is
        silently dropped as a duplicate)."""
        source_id = "/Users/x/notes/a.md"
        text = "Hello, this is a test note."
        h = hashlib.sha256(f"{source_id}|{text}".encode()).hexdigest()
        content_hash = f"{h}-0"
        assert content_hash.rsplit("-", 1)[0] == h
        # Same text under a different source_id → different base hash, no collision.
        other = hashlib.sha256(f"/Users/x/notes/b.md|{text}".encode()).hexdigest()
        assert other != h

    def test_per_message_hash_imessage(self):
        """iMessage uses sha256(chat_id:msg_id:text) per message."""
        chat_id = "chat123"
        msg_id = "42"
        text = "Hey!"
        combined = f"{chat_id}:{msg_id}:{text}"
        h = hashlib.sha256(combined.encode()).hexdigest()
        content_hash = f"{h}-0"
        base = content_hash.rsplit("-", 1)[0]
        assert base == h
        assert content_hash.endswith("-0")

    def test_whatsapp_window_id_hash(self):
        """WhatsApp uses sha256(window_id) where window_id = chat_id:first_ts.
        This is NOT content-addressed — same content in different windows → different hash."""
        window_id = "chat-abc:2024-06-15T10:00:00"
        h = hashlib.sha256(window_id.encode()).hexdigest()
        content_hash = f"{h}-0"
        h2 = hashlib.sha256(window_id.encode()).hexdigest()
        assert h == h2
        assert content_hash.startswith(h)

    def test_base_hash_extraction(self):
        """The dedup logic in tools.py extracts base hash by splitting on last '-'."""
        content_hash = "abc123def456-3"
        base = content_hash.rsplit("-", 1)[0]
        assert base == "abc123def456"

    def test_base_hash_no_index(self):
        """Hash without chunk index."""
        content_hash = "abc123"
        if "-" in content_hash:
            base = content_hash.rsplit("-", 1)[0]
        else:
            base = content_hash
        assert base == "abc123"

    def test_different_base_triggers_replace(self):
        """Same source_id with different base hash should trigger replacement."""
        hash_v1 = "aaa111-0"
        hash_v2 = "bbb222-0"
        base_v1 = hash_v1.rsplit("-", 1)[0]
        base_v2 = hash_v2.rsplit("-", 1)[0]
        assert base_v1 != base_v2  # Different content → replace

    def test_same_base_different_index_no_replace(self):
        """Same base hash, different chunk index → same content, should NOT replace."""
        hash_c0 = "aaa111-0"
        hash_c1 = "aaa111-1"
        base_c0 = hash_c0.rsplit("-", 1)[0]
        base_c1 = hash_c1.rsplit("-", 1)[0]
        assert base_c0 == base_c1  # Same base → skip


# ── _parse_date_ts edge cases ──────────────────────────────────────────────


class TestParseDateEdgeCases:
    def test_microseconds(self):
        dt = _parse_date_ts("2024-06-15T10:30:00.123456Z")
        assert dt is not None
        assert dt.microsecond == 123456

    def test_date_with_time_no_seconds(self):
        dt = _parse_date_ts("2024-06-15T10:30Z")
        assert dt is not None
        assert dt.year == 2024
        assert dt.month == 6
        assert dt.day == 15
        assert dt.hour == 10
        assert dt.minute == 30

    def test_extremely_old_date(self):
        dt = _parse_date_ts("1900-01-01T00:00:00Z")
        assert dt is not None
        assert dt.year == 1900

    def test_far_future_date(self):
        dt = _parse_date_ts("2099-12-31T23:59:59Z")
        assert dt is not None
        assert dt.year == 2099

    def test_timezone_preserved(self):
        dt = _parse_date_ts("2024-06-15T10:00:00+05:30")
        assert dt is not None
        # The offset should be preserved
        assert dt.utcoffset().total_seconds() == 5.5 * 3600

    def test_multiple_z_formats(self):
        # Standard Z suffix
        d1 = _parse_date_ts("2024-01-01T00:00:00Z")
        d2 = _parse_date_ts("2024-01-01T00:00:00+00:00")
        assert d1 is not None and d2 is not None
        assert d1 == d2
