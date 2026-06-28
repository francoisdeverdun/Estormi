"""Unit tests for the extracted Apple-connector ingest modules.

The per-record ingest logic for the four Apple connectors (iMessage, Apple Mail,
Apple Notes, Reminders) used to live inside ``python3 - <<'PYEOF'`` heredocs in
their ``watch_and_ingest.sh`` scripts — bodies the test suite never executed,
which is exactly how a ``post_chunks`` ``TypeError`` once shipped to a 03:00 run.
The bodies are now importable modules (``estormi_ingestion.<source>.ingest`` plus
``estormi_ingestion.reminders.mark_complete``); these tests import them and drive
``main()`` end-to-end with the HTTP POST stubbed, asserting the posted payload,
the exit codes, and (for notes / reminders-mark-complete) the stdout contract the
shell captures.
"""

from __future__ import annotations

import hashlib
import io
import json
import sqlite3
from contextlib import redirect_stdout
from unittest.mock import MagicMock, patch

import pytest

from estormi_ingestion.apple_mail import ingest as mail_ingest
from estormi_ingestion.apple_notes import ingest as notes_ingest
from estormi_ingestion.imessage import ingest as imessage_ingest
from estormi_ingestion.reminders import ingest as reminders_ingest
from estormi_ingestion.reminders import mark_complete as reminders_mark
from estormi_ingestion.shared import emit

pytestmark = pytest.mark.unit


def _capture_posts():
    """Patch ``post_chunk`` to record payloads and reply ``status: ok``."""
    sent: list[dict] = []

    def fake_post(url, payload, **_):
        sent.append(payload)
        return MagicMock(json=lambda: {"status": "ok"})

    return sent, patch.object(emit.http_client, "post_chunk", side_effect=fake_post)


def _write(tmp_path, name, content):
    p = tmp_path / name
    p.write_text(content if isinstance(content, str) else json.dumps(content))
    return p


# ── iMessage ────────────────────────────────────────────────────────────────


class TestIMessageIngest:
    def _run(self, tmp_path, meta, body, *, chunk_size="800", chunk_overlap="100"):
        meta_f = _write(tmp_path, "m.meta.json", meta)
        body_f = _write(tmp_path, "m.txt", body)
        sent, ctx = _capture_posts()
        with ctx:
            rc = imessage_ingest.main(
                ["-", str(meta_f), str(body_f), "http://x/", "/repo", chunk_size, chunk_overlap]
            )
        return rc, sent

    def test_payload_wires_chat_id_raw_to_chat_id(self, tmp_path):
        """chat_id_raw must be the chat GUID, as a TOP-LEVEL ingest field — this is
        the exact contract a TypeError once broke when post_chunks lost the param."""
        rc, sent = self._run(
            tmp_path,
            {"id": "g1", "chat_id": "iMessage;-;chat42", "chat_name": "Fam", "timestamp_iso": "T"},
            "hello there",
        )
        assert rc == 0
        assert sent and sent[0]["chat_id_raw"] == "iMessage;-;chat42"
        assert "chat_id_raw" not in sent[0].get("meta", {})
        assert sent[0]["source"] == "imessage"
        assert sent[0]["source_id"] == "g1"
        assert sent[0]["title"] == "iMessage — Fam"

    def test_title_falls_back_to_name_without_chat_name(self, tmp_path):
        rc, sent = self._run(tmp_path, {"id": "g2", "name": "Bob", "timestamp_iso": "T"}, "yo")
        assert rc == 0 and sent[0]["title"] == "iMessage — Bob"

    def test_base_hash_distinct_across_chats_for_same_text(self, tmp_path):
        """content_hash folds chat_id:msg_id:text so identical short messages in
        two chats do not collide and get deduped to one."""
        _, a = self._run(tmp_path, {"id": "x", "chat_id": "A", "timestamp_iso": "T"}, "ok")
        _, b = self._run(tmp_path, {"id": "x", "chat_id": "B", "timestamp_iso": "T"}, "ok")
        assert a[0]["content_hash"] != b[0]["content_hash"]

    def test_empty_body_posts_nothing_and_exits_clean(self, tmp_path):
        rc, sent = self._run(tmp_path, {"id": "g", "timestamp_iso": "T"}, "   \n  ")
        assert rc == 0 and sent == []

    def test_otp_message_is_dropped(self, tmp_path):
        rc, sent = self._run(
            tmp_path, {"id": "g", "timestamp_iso": "T"}, "Your verification code is 482913"
        )
        assert rc == 0 and sent == []

    def test_failed_post_returns_exit_1(self, tmp_path):
        """A POST that the server rejects must exit 1 so the shell keeps the staged
        files and holds the watermark back."""
        meta_f = _write(tmp_path, "m.meta.json", {"id": "g", "chat_id": "c", "timestamp_iso": "T"})
        body_f = _write(tmp_path, "m.txt", "important message")

        def fail_post(url, payload, **_):
            return MagicMock(json=lambda: {"status": "error"})

        with patch.object(emit.http_client, "post_chunk", side_effect=fail_post):
            rc = imessage_ingest.main(
                ["-", str(meta_f), str(body_f), "http://x/", "/repo", "800", "100"]
            )
        assert rc == 1


# ── Apple Mail ────────────────────────────────────────────────────────────────


class TestMailIngest:
    def _run(self, tmp_path, meta, body):
        meta_f = _write(tmp_path, "mail.meta.json", meta)
        body_f = _write(tmp_path, "mail.txt", body)
        sent, ctx = _capture_posts()
        with ctx:
            rc = mail_ingest.main(
                ["-", str(meta_f), str(body_f), "http://x/", "/repo", "1000", "150"]
            )
        return rc, sent

    def test_chat_id_raw_is_thread_root_from_first_references_id(self, tmp_path):
        headers = (
            "References: <root@example.com> <r2@example.com>\nMessage-ID: <self@example.com>\n"
        )
        rc, sent = self._run(
            tmp_path,
            {"id": "mail1", "title": "T", "date": "D", "from": "a@example.com", "headers": headers},
            "body one\n\nbody two with enough length here",
        )
        assert rc == 0
        assert sent[0]["chat_id_raw"] == hashlib.sha256(b"root@example.com").hexdigest()

    def test_header_is_prepended_and_pii_filtered_after_header(self, tmp_path):
        """The From/Subject header is prepended into the indexed text, and PII is
        filtered AFTER prepending — so a sender email in the header is redacted
        too (it could leak the same data a phone in the body would)."""
        rc, sent = self._run(
            tmp_path,
            {"id": "mail2", "title": "Budget", "date": "D", "from": "boss@example.com"},
            "the body text here",
        )
        assert rc == 0
        joined = "".join(c["text"] for c in sent)
        assert "From: " in joined and "Subject: Budget" in joined
        # The sender email in the header is redacted by the post-header PII pass.
        assert "boss@example.com" not in joined
        assert "[REDACTED:EMAIL]" in joined

    def test_otp_mail_dropped(self, tmp_path):
        rc, sent = self._run(
            tmp_path,
            {"id": "m", "title": "Code", "from": "x@example.com"},
            "Your one-time code is 992834, do not share it",
        )
        assert rc == 0 and sent == []

    def test_empty_body_no_post(self, tmp_path):
        rc, sent = self._run(tmp_path, {"id": "m"}, "  \n\n  ")
        assert rc == 0 and sent == []


class TestMailThreadRootKey:
    """``thread_root_key`` is the real production function, imported directly
    (it used to be replicated verbatim into a test because it lived in a heredoc)."""

    @staticmethod
    def _key(message_id):
        return hashlib.sha256(message_id.encode("utf-8")).hexdigest()

    def test_references_uses_first_message_id(self):
        headers = (
            "From: alice@example.com\n"
            "References: <root@example.com> <reply1@example.com> <reply2@example.com>\n"
            "In-Reply-To: <reply2@example.com>\n"
            "Message-ID: <reply3@example.com>\n"
        )
        assert mail_ingest.thread_root_key(headers, "src-1") == self._key("root@example.com")

    def test_in_reply_to_used_when_no_references(self):
        headers = "In-Reply-To: <parent@example.com>\nMessage-ID: <child@example.com>\n"
        assert mail_ingest.thread_root_key(headers, "src-2") == self._key("parent@example.com")

    def test_message_id_used_when_no_references_or_in_reply_to(self):
        headers = "From: carol@example.com\nMessage-ID: <self@example.com>\n"
        assert mail_ingest.thread_root_key(headers, "src-3") == self._key("self@example.com")

    def test_falls_back_to_source_id_when_no_headers(self):
        assert mail_ingest.thread_root_key("", "src-4") == "src-4"
        assert mail_ingest.thread_root_key(None, "src-5") == "src-5"

    def test_header_parsing_is_case_insensitive(self):
        assert mail_ingest.thread_root_key("rEfErEnCeS: <root@example.com>\n", "s6") == self._key(
            "root@example.com"
        )

    def test_garbled_headers_fall_back_to_source_id(self):
        assert mail_ingest.thread_root_key("not a header block at all", "src-7") == "src-7"
        assert mail_ingest.thread_root_key("References:\nIn-Reply-To:\n", "src-8") == "src-8"


# ── Apple Notes ───────────────────────────────────────────────────────────────


class TestNotesIngest:
    def _run(self, tmp_path, meta, raw_html, *, chunk_size="900"):
        meta_f = _write(tmp_path, "n.meta.json", meta)
        html_f = _write(tmp_path, "n.html", raw_html)
        sent, ctx = _capture_posts()
        buf = io.StringIO()
        with ctx, redirect_stdout(buf):
            rc = notes_ingest.main(
                ["-", str(meta_f), str(html_f), "http://x/", "/repo", chunk_size]
            )
        return rc, sent, buf.getvalue().strip()

    def test_stdout_is_bare_chunk_count_the_shell_captures(self, tmp_path):
        """The shell captures stdout into CHUNKS_FOR_NOTE — it must be exactly the
        chunk count (an int) and nothing else."""
        body = "<p>" + ("alpha " * 40) + "</p><p>" + ("beta " * 40) + "</p>"
        rc, sent, out = self._run(tmp_path, {"id": "n1", "title": "T", "date": "D"}, body)
        assert rc == 0
        assert out.isdigit(), f"stdout must be a bare int, got {out!r}"
        assert int(out) == len(sent)
        assert sent and sent[0]["source"] == "notes"
        assert "chat_id_raw" not in sent[0]

    def test_empty_note_prints_zero_no_post(self, tmp_path):
        rc, sent, out = self._run(tmp_path, {"id": "n", "title": "T"}, "<p>   </p>")
        assert rc == 0 and out == "0" and sent == []

    def test_otp_note_prints_zero_no_post(self, tmp_path):
        rc, sent, out = self._run(tmp_path, {"id": "n", "title": "T"}, "<p>Your code is 771204</p>")
        assert rc == 0 and out == "0" and sent == []

    def test_html_block_tags_become_paragraph_breaks(self, tmp_path):
        """Block tags map to \\n\\n so paragraph_chunks splits sections rather than
        fusing them — long enough sections produce distinct chunks."""
        body = "<h1>" + ("Head " * 30) + "</h1><p>" + ("para " * 60) + "</p>"
        rc, sent, out = self._run(tmp_path, {"id": "n2", "title": "T", "date": "D"}, body)
        assert rc == 0 and int(out) == len(sent) and len(sent) >= 1

    def test_failed_post_returns_1(self, tmp_path):
        meta_f = _write(tmp_path, "n.meta.json", {"id": "n", "title": "T"})
        html_f = _write(tmp_path, "n.html", "<p>" + ("real content " * 30) + "</p>")

        def fail_post(url, payload, **_):
            return MagicMock(json=lambda: {"status": "error"})

        buf = io.StringIO()
        with patch.object(emit.http_client, "post_chunk", side_effect=fail_post):
            with redirect_stdout(buf):
                rc = notes_ingest.main(["-", str(meta_f), str(html_f), "http://x/", "/repo", "900"])
        assert rc == 1
        # On failure the chunk count is NOT printed (the shell discards the capture).
        assert buf.getvalue().strip() == ""


# ── Reminders (ingest) ────────────────────────────────────────────────────────


class TestRemindersIngest:
    def _run(self, tmp_path, meta, body):
        meta_f = _write(tmp_path, "r.meta.json", meta)
        body_f = _write(tmp_path, "r.txt", body)
        sent, ctx = _capture_posts()
        with ctx:
            rc = reminders_ingest.main(["-", str(meta_f), str(body_f), "http://x/", "/repo"])
        return rc, sent

    def test_reminder_posted_whole_as_single_chunk(self, tmp_path):
        rc, sent = self._run(
            tmp_path, {"id": "r1", "title": "Pay rent", "date": "D"}, "Pay   the\nrent"
        )
        assert rc == 0
        assert len(sent) == 1
        assert sent[0]["source"] == "reminders"
        assert sent[0]["source_id"] == "r1"
        # \s+ collapsed to single spaces
        assert sent[0]["text"] == "Pay the rent"
        assert "chat_id_raw" not in sent[0]

    def test_empty_reminder_no_post(self, tmp_path):
        rc, sent = self._run(tmp_path, {"id": "r"}, "   ")
        assert rc == 0 and sent == []

    def test_otp_reminder_dropped(self, tmp_path):
        rc, sent = self._run(tmp_path, {"id": "r"}, "Your verification code is 123456")
        assert rc == 0 and sent == []

    def test_failed_post_returns_1(self, tmp_path):
        meta_f = _write(tmp_path, "r.meta.json", {"id": "r", "title": "T"})
        body_f = _write(tmp_path, "r.txt", "buy milk")

        def fail_post(url, payload, **_):
            return MagicMock(json=lambda: {"status": "error"})

        with patch.object(emit.http_client, "post_chunk", side_effect=fail_post):
            rc = reminders_ingest.main(["-", str(meta_f), str(body_f), "http://x/", "/repo"])
        assert rc == 1


# ── Reminders (mark-complete) ─────────────────────────────────────────────────


class TestRemindersMarkComplete:
    def _db(self, tmp_path, rows):
        db = tmp_path / "estormi.db"
        conn = sqlite3.connect(db)
        conn.execute(
            "CREATE TABLE chunks (id INTEGER PRIMARY KEY, source TEXT, source_id TEXT, "
            "completed INTEGER DEFAULT 0)"
        )
        conn.executemany("INSERT INTO chunks (source, source_id, completed) VALUES (?, ?, ?)", rows)
        conn.commit()
        conn.close()
        return db

    def _run(self, db, exported_ids):
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = reminders_mark.main(["-", json.dumps(list(exported_ids)), str(db)])
        return rc, buf.getvalue()

    def _completed(self, db):
        conn = sqlite3.connect(db)
        out = {r[0] for r in conn.execute("SELECT source_id FROM chunks WHERE completed = 1")}
        conn.close()
        return out

    def test_marks_only_reminders_absent_from_export(self, tmp_path):
        db = self._db(
            tmp_path,
            [
                ("reminders", "still-here", 0),
                ("reminders", "gone", 0),
                ("notes", "untouched", 0),  # other source must not be touched
            ],
        )
        rc, out = self._run(db, {"still-here"})
        assert rc == 0
        assert "Marked 1 reminder(s) as completed" in out
        assert self._completed(db) == {"gone"}

    def test_no_output_when_nothing_newly_completed(self, tmp_path):
        db = self._db(tmp_path, [("reminders", "a", 0), ("reminders", "b", 0)])
        rc, out = self._run(db, {"a", "b"})
        assert rc == 0 and out.strip() == ""
        assert self._completed(db) == set()

    def test_already_completed_rows_are_left_alone(self, tmp_path):
        db = self._db(tmp_path, [("reminders", "old", 1), ("reminders", "new", 0)])
        rc, out = self._run(db, set())
        assert rc == 0
        # "old" was already completed (not re-counted); only "new" is newly marked.
        assert "Marked 1 reminder(s) as completed" in out
        assert self._completed(db) == {"old", "new"}

    def test_adds_completed_column_if_missing(self, tmp_path):
        """The idempotent ALTER TABLE migration must add the column when the MCP
        server has not yet restarted with the new schema."""
        db = tmp_path / "estormi.db"
        conn = sqlite3.connect(db)
        conn.execute("CREATE TABLE chunks (id INTEGER PRIMARY KEY, source TEXT, source_id TEXT)")
        conn.executemany(
            "INSERT INTO chunks (source, source_id) VALUES ('reminders', ?)",
            [("keep",), ("drop",)],
        )
        conn.commit()
        conn.close()
        rc, out = self._run(db, {"keep"})
        assert rc == 0
        assert self._completed(db) == {"drop"}
