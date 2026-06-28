"""Tests for memory_core/audit.py — structured audit logging."""

from __future__ import annotations

import importlib
import json
import os
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit


class TestLogToolCall:
    @pytest.fixture(autouse=True)
    def _tmp_audit_log(self, tmp_path, monkeypatch):
        """Redirect audit log to a temp file for each test.

        `memory_core.audit` lazily opens the log file the first time
        `log_tool_call` is invoked. Reload both `memory_core.settings` and
        `memory_core.audit` so the fresh logger picks up `AUDIT_LOG_PATH`.
        """
        self.log_path = str(tmp_path / "audit-test.log")
        orig_audit_path = os.environ.get("AUDIT_LOG_PATH")
        monkeypatch.setenv("AUDIT_LOG_PATH", self.log_path)

        from memory_core import audit as core_audit
        from memory_core import settings as core_settings

        importlib.reload(core_settings)
        importlib.reload(core_audit)

        self.log_tool_call = core_audit.log_tool_call

        yield

        # Restore the original AUDIT_LOG_PATH and reload once more so the
        # modules don't carry this test's temp-file state into the next one.
        if orig_audit_path is None:
            os.environ.pop("AUDIT_LOG_PATH", None)
        else:
            os.environ["AUDIT_LOG_PATH"] = orig_audit_path
        importlib.reload(core_settings)
        importlib.reload(core_audit)

    def test_log_tool_call_writes_json_line(self):
        self.log_tool_call(
            token_sub="user-123",
            token_email="user@test.com",
            tool_name="search_memory",
            query="what happened today?",
            result_ids=["id-1", "id-2"],
            duration_ms=42.5,
        )
        content = Path(self.log_path).read_text().strip()
        lines = content.strip().split("\n")
        assert len(lines) >= 1
        entry = json.loads(lines[-1])
        assert entry["tool"] == "search_memory"
        assert entry["subject_hash"]
        assert entry["email_hash"]
        assert "user-123" not in content
        assert "user@test.com" not in content
        assert entry["result_count"] == 2
        assert len(entry["result_id_hashes"]) == 2
        assert entry["duration_ms"] == 42.5
        assert "what happened today?" not in content

    def test_log_tool_call_with_error(self):
        self.log_tool_call(
            token_sub="u",
            token_email="e",
            tool_name="ingest_chunk",
            query="[ingest:test]",
            result_ids=[],
            duration_ms=10.0,
            error="timeout",
        )
        content = Path(self.log_path).read_text().strip()
        entry = json.loads(content.split("\n")[-1])
        assert entry["error"] is True
        assert entry["error_hash"]
        assert "timeout" not in content

    def test_log_hashes_query_instead_of_storing_raw_text(self):
        long_query = "x" * 500
        self.log_tool_call(
            token_sub="u",
            token_email="e",
            tool_name="test",
            query=long_query,
            result_ids=[],
            duration_ms=1.0,
        )
        content = Path(self.log_path).read_text().strip()
        entry = json.loads(content.split("\n")[-1])
        assert entry["query_chars"] == 500
        assert entry["query_hash"]
        assert "x" * 200 not in content

    def test_log_hashes_and_limits_result_ids_to_10(self):
        ids = [f"id-{i}" for i in range(25)]
        self.log_tool_call(
            token_sub="u",
            token_email="e",
            tool_name="test",
            query="q",
            result_ids=ids,
            duration_ms=1.0,
        )
        content = Path(self.log_path).read_text().strip()
        entry = json.loads(content.split("\n")[-1])
        assert len(entry["result_id_hashes"]) == 10
        assert entry["result_count"] == 25
        assert "id-0" not in content

    def test_log_includes_timestamp(self):
        self.log_tool_call(
            token_sub="u",
            token_email="e",
            tool_name="test",
            query="q",
            result_ids=[],
            duration_ms=1.0,
        )
        content = Path(self.log_path).read_text().strip()
        entry = json.loads(content.split("\n")[-1])
        assert "timestamp" in entry


class TestRotation:
    """The audit log must not grow unbounded — long-lived Mac installs would
    otherwise leak megabytes per day. Once it crosses ``AUDIT_LOG_MAX_BYTES``
    the live file is renamed to ``<path>.1`` and a fresh empty file takes
    its place.
    """

    @pytest.fixture(autouse=True)
    def _tiny_log(self, tmp_path, monkeypatch):
        self.log_path = str(tmp_path / "rotate-test.log")
        orig_audit_path = os.environ.get("AUDIT_LOG_PATH")
        orig_max_bytes = os.environ.get("AUDIT_LOG_MAX_BYTES")
        monkeypatch.setenv("AUDIT_LOG_PATH", self.log_path)
        # Force rotation after the first few lines.
        monkeypatch.setenv("AUDIT_LOG_MAX_BYTES", "2048")

        from memory_core import audit as core_audit
        from memory_core import settings as core_settings

        importlib.reload(core_settings)
        importlib.reload(core_audit)
        self.log_tool_call = core_audit.log_tool_call

        yield

        # Restore the original env and reload once more so module state is
        # deterministic between tests.
        for name, value in (
            ("AUDIT_LOG_PATH", orig_audit_path),
            ("AUDIT_LOG_MAX_BYTES", orig_max_bytes),
        ):
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        importlib.reload(core_settings)
        importlib.reload(core_audit)

    def test_rotation_caps_live_file_size(self):
        # Write enough lines to cross the 2 KiB threshold many times over.
        for _ in range(200):
            self.log_tool_call(
                token_sub="u",
                token_email="e",
                tool_name="search_memory",
                query="x" * 100,
                result_ids=["a", "b", "c"],
                duration_ms=1.0,
            )
        live = Path(self.log_path)
        backup = Path(self.log_path + ".1")
        # Live file is strictly bounded by the rotation cap + one final line.
        # The rotation check fires every ~1 KiB of writes, so the file can
        # exceed the cap by at most a small amount.
        assert live.exists()
        assert live.stat().st_size < 2048 + 4096
        # At least one rotation should have produced a backup.
        assert backup.exists()


class TestSecurityDecisionLog:
    """``log_security_decision`` is the audit trail for middleware rejections
    (bad bearer, CSRF, forwarded-without-token). It must record the decision
    and hash the client host — never store the raw host or query content.
    """

    @pytest.fixture(autouse=True)
    def _tmp_audit_log(self, tmp_path, monkeypatch):
        self.log_path = str(tmp_path / "sec-audit.log")
        orig = os.environ.get("AUDIT_LOG_PATH")
        monkeypatch.setenv("AUDIT_LOG_PATH", self.log_path)
        from memory_core import audit as core_audit
        from memory_core import settings as core_settings

        importlib.reload(core_settings)
        importlib.reload(core_audit)
        self.log_security_decision = core_audit.log_security_decision
        yield
        if orig is None:
            os.environ.pop("AUDIT_LOG_PATH", None)
        else:
            os.environ["AUDIT_LOG_PATH"] = orig
        importlib.reload(core_settings)
        importlib.reload(core_audit)

    def test_records_decision_and_hashes_host(self):
        self.log_security_decision(
            decision="reject",
            path="/api/admin/reset",
            client_host="192.168.1.50",
            reason="bearer_mismatch",
            method="POST",
        )
        content = Path(self.log_path).read_text().strip()
        entry = json.loads(content.split("\n")[-1])
        assert entry["decision"] == "reject"
        assert entry["path"] == "/api/admin/reset"
        assert entry["method"] == "POST"
        assert entry["reason"] == "bearer_mismatch"
        assert entry["client_hash"]
        # The raw client host must never appear in the log.
        assert "192.168.1.50" not in content

    def test_blank_host_and_method_default_safely(self):
        self.log_security_decision(
            decision="accept",
            path="/api/settings",
            client_host="",
            reason="loopback",
        )
        entry = json.loads(Path(self.log_path).read_text().strip().split("\n")[-1])
        assert entry["method"] == ""
        assert entry["client_hash"] == ""


class TestDigest:
    """``_digest`` must hash any input and never leak the raw value, even when
    callers hand it a non-string (a defensive path for test wiring)."""

    def test_empty_returns_empty(self):
        from memory_core.audit import _digest

        assert _digest("") == ""
        assert _digest(b"") == ""

    def test_str_and_bytes_agree(self):
        from memory_core.audit import _digest

        assert _digest("hello") == _digest(b"hello")
        assert len(_digest("hello")) == 16

    def test_non_string_is_coerced_not_crashed(self):
        from memory_core.audit import _digest

        # A MagicMock-like object: must hash a string repr, never raise.
        assert _digest(12345) == _digest("12345")


class TestRotatingFileDegradesGracefully:
    """The audit writer is best-effort: an unopenable path or a failing write
    must fall back to stderr and never raise into the caller — losing an audit
    line is acceptable, crashing a tool call is not.
    """

    def test_unopenable_path_falls_back_to_stderr(self, tmp_path, capsys):
        from memory_core.audit import _RotatingAuditFile

        # Point at a path whose parent is a file, so mkdir/open fails.
        blocker = tmp_path / "afile"
        blocker.write_text("x")
        writer = _RotatingAuditFile(str(blocker / "nested" / "audit.log"), 1024)
        n = writer.write("audit line\n")
        assert n == len("audit line\n")
        # The line went to stderr, not a crash.
        assert "audit line" in capsys.readouterr().err

    def test_write_after_close_falls_back_to_stderr(self, tmp_path, capsys):
        from memory_core.audit import _RotatingAuditFile

        writer = _RotatingAuditFile(str(tmp_path / "audit.log"), 1024)
        writer.close()
        writer.write("post-close line\n")
        assert "post-close line" in capsys.readouterr().err
        # close() is idempotent.
        writer.close()
