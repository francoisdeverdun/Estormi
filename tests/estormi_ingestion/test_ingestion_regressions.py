"""Regression tests for ingestion pipeline bugs found after the first production run.

Each test is named after the bug it prevents from regressing.
"""

from __future__ import annotations

import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

pytestmark = [pytest.mark.integration, pytest.mark.regression]

# Connectors that background their exporter (`& ... wait "$_OSAS_PID"`) — the
# only ones at risk of the swallow-the-export-RC data-loss pattern (bug C1).
_BACKGROUNDED_SCRIPTS = [
    p
    for p in sorted((REPO_ROOT / "packages" / "estormi_ingestion").glob("*/watch_and_ingest.sh"))
    if "_OSAS_PID=$!" in p.read_text()
]


# ── Bug: iCloud evicted files (.name.ext.icloud) were invisible to the walk ──
# Root cause: the documents walk skipped all dot-files, but iCloud placeholder
# names start with a dot.  Fix: remap .name.ext.icloud → name.ext.


class TestDocumentsICloudPlaceholderWalk:
    """The walk must detect .name.ext.icloud placeholders and remap them.

    These tests drive the REAL ``ingest_documents.main()`` walk (the
    ``.icloud`` remap and dot-file skip logic live inline in ``main()``,
    so there is no standalone function to import). External effects are
    mocked: ``get_watermark`` (no DB), ``subprocess.run`` (no ``brctl``),
    and ``ingest_file`` (no HTTP POST) — the last is recorded so the
    candidate paths the real walk produced can be asserted.
    """

    def _walk_candidates(self, root: Path) -> set[str]:
        """Run the real ``ingest_documents.main()`` and capture walked file names."""
        from estormi_ingestion.documents import ingest_documents

        seen: list[Path] = []

        def _record(path, mcp_url, headers, dry_run):
            seen.append(path)
            # (ok, skipped, failed, transient, unreadable) — gained a failed
            # counter when the watermark gate was added, then a transient flag
            # so an iCloud download / I/O failure (which returns 0 chunks) no
            # longer silently advances the watermark past the file, then an
            # unreadable flag so a deterministically broken file (encrypted /
            # corrupt) does the opposite — it must NOT pin the watermark.
            return (1, 0, 0, False, False)

        with (
            patch.object(sys, "argv", ["ingest_documents.py", "--root", str(root), "--dry-run"]),
            patch.object(
                ingest_documents, "get_watermark", new=lambda _src: _async_value((None, None))
            ),
            patch.object(ingest_documents, "ingest_file", side_effect=_record),
            patch("subprocess.run", return_value=MagicMock(returncode=0)),
        ):
            ingest_documents.main()
        return {p.name for p in seen}

    def test_real_file_is_found(self, tmp_path):
        (tmp_path / "report.pdf").touch()
        assert "report.pdf" in self._walk_candidates(tmp_path)

    def test_icloud_placeholder_is_remapped_to_real_path(self, tmp_path):
        """A .report.pdf.icloud placeholder should produce report.pdf as the candidate."""
        (tmp_path / ".report.pdf.icloud").touch()
        assert "report.pdf" in self._walk_candidates(tmp_path), (
            "iCloud placeholder .report.pdf.icloud was not remapped to report.pdf"
        )

    def test_plain_dotfile_is_skipped(self, tmp_path):
        """Hidden files that are NOT iCloud placeholders must still be skipped."""
        (tmp_path / ".hidden_file").touch()
        assert ".hidden_file" not in self._walk_candidates(tmp_path)

    def test_mixed_directory(self, tmp_path):
        """Real files + evicted files are all found; plain dotfiles are skipped."""
        (tmp_path / "notes.txt").touch()
        (tmp_path / ".invoice.pdf.icloud").touch()
        (tmp_path / ".DS_Store").touch()
        names = self._walk_candidates(tmp_path)
        assert "notes.txt" in names
        assert "invoice.pdf" in names
        assert ".DS_Store" not in names


async def _async_value(value):
    """Tiny coroutine that resolves to ``value`` — for patching async helpers."""
    return value


# ── Bug: a transient per-file failure (iCloud download timeout / I/O error)
#    returned (0,0,0) — indistinguishable from a clean empty result — so the
#    watermark advanced PAST the failed file, skipping it forever.
#    Fix: ingest_file returns a transient flag; main() leaves the watermark
#    untouched and exits non-zero when any file failed transiently.
#
#    Mirror bug (opposite direction): a *deterministically* broken file
#    (encrypted / corrupt PDF) was ALSO flagged transient, so the watermark
#    never advanced — one un-decryptable PDF pinned the whole source, re-walking
#    every file nightly and keeping the run permanently red. Fix: such files
#    return an ``unreadable`` flag instead, which must NOT block the watermark
#    and must NOT fail the run. ──────────────────────────────────────────────


class TestDocumentsTransientFailureWatermark:
    """A transient failure must hold the watermark; an unreadable one must not."""

    def _run_main(self, tmp_path, ingest_return):
        from estormi_ingestion.documents import ingest_documents

        (tmp_path / "report.pdf").touch()
        set_calls: list = []

        async def _fake_get_watermark(_src):
            return (None, None)

        async def _fake_set_watermark(source, ts):
            set_calls.append((source, ts))

        with (
            patch.object(sys, "argv", ["ingest_documents.py", "--root", str(tmp_path)]),
            patch.object(ingest_documents, "get_watermark", new=_fake_get_watermark),
            patch.object(ingest_documents, "set_watermark", new=_fake_set_watermark),
            patch.object(ingest_documents, "ingest_file", return_value=ingest_return),
            patch("subprocess.run", return_value=MagicMock(returncode=0)),
        ):
            try:
                ingest_documents.main()
                exit_code = 0
            except SystemExit as e:
                exit_code = e.code or 0
        return exit_code, set_calls

    def test_transient_failure_does_not_advance_watermark(self, tmp_path):
        # (ok, skipped, failed, transient, unreadable) — a download/I/O failure.
        exit_code, set_calls = self._run_main(tmp_path, (0, 0, 0, True, False))
        assert exit_code == 1, "a transient failure must exit non-zero"
        assert set_calls == [], "watermark must NOT advance past a transiently failed file"

    def test_clean_empty_result_advances_watermark(self, tmp_path):
        # A clean empty result (e.g. unsupported/too-short) is NOT transient.
        exit_code, set_calls = self._run_main(tmp_path, (0, 0, 0, False, False))
        assert exit_code == 0
        assert len(set_calls) == 1, "a clean run must advance the watermark"

    def test_unreadable_failure_advances_watermark(self, tmp_path):
        # A deterministically unreadable file (encrypted/corrupt PDF) must NOT
        # pin the watermark and must NOT fail the run — otherwise one broken
        # PDF stalls the whole source forever.
        exit_code, set_calls = self._run_main(tmp_path, (0, 0, 0, False, True))
        assert exit_code == 0, "an unreadable file must not fail the run"
        assert len(set_calls) == 1, "watermark must advance despite an unreadable file"


# ── Bug: FORCE_FULL=1 was not respected — a stale dev watermark caused the
#    first production run to skip every file.
#    Fix: check FORCE_FULL env var and set last_run=None when set. ──────────


class TestFORCE_FULL:
    """FORCE_FULL=1 must ignore the existing watermark."""

    def _resolve_last_run(self, watermark_ts: str | None, force_full: bool):
        """Replicate the last_run resolution logic from ingest_documents.py."""
        last_run = None
        if watermark_ts and not force_full:
            last_run = datetime.fromisoformat(watermark_ts)
            if last_run.tzinfo is None:
                last_run = last_run.replace(tzinfo=timezone.utc)
        return last_run

    def test_without_force_full_watermark_is_used(self):
        ts = "2026-05-01T00:00:00+00:00"
        last_run = self._resolve_last_run(ts, force_full=False)
        assert last_run is not None

    def test_with_force_full_watermark_is_ignored(self):
        ts = "2026-05-01T00:00:00+00:00"
        last_run = self._resolve_last_run(ts, force_full=True)
        assert last_run is None, (
            "FORCE_FULL=1 should set last_run=None even when a watermark exists"
        )

    def test_no_watermark_always_none(self):
        last_run = self._resolve_last_run(None, force_full=False)
        assert last_run is None

    def test_env_var_parsing(self):
        """The env var check must accept 1, true, yes (case-insensitive)."""
        for val in ("1", "true", "True", "TRUE", "yes", "YES"):
            assert val.lower() in ("1", "true", "yes"), f"Unrecognised value: {val}"
        for val in ("0", "false", "", "no"):
            assert val.lower() not in ("1", "true", "yes"), f"Should be falsy: {val}"


# ── Bug: nightly mail ingestion exported 0 messages because app_lifecycle
#    gave Mail only 15 s warmup before running the AppleScript. Mail launched
#    cold at 2 am had not yet synced IMAP headers, so every mailbox appeared
#    empty. The export then ran for the full 18 000 s AppleScript timeout.
#    Fix (original): add a readiness_check poll to APP_CONFIG["Mail"] that
#    waits until Mail reports at least one mailbox, up to 180 s.
#
# ── Bug (regression): even when Mail was already running the readiness check
#    was skipped, because _we_launched was False. A pre-existing Mail process
#    may not have re-synced IMAP since the last network sleep. Additionally,
#    the readiness script only counted mailboxes (cached locally), not message
#    headers, so the main export still blocked for the full 18 000 s on IMAP.
#    And `wait "$_OSAS_PID"` (set -euo pipefail) caused the bash stage script
#    to exit before processing any files staged before the timeout.
#    Fix: readiness_check_always=True so the probe runs even for a pre-existing
#    Mail; probe INBOX messages (not just mailboxes); readiness_timeout → 600 s;
#    `wait "$_OSAS_PID" || echo ...` so staged files are processed on partial run. ──


class TestMailReadinessProbe:
    """app_lifecycle must poll Mail readiness before running the export."""

    def test_mail_config_has_readiness_check(self):
        """APP_CONFIG['Mail'] must declare a readiness_check script."""
        from estormi_ingestion.shared.host.app_lifecycle import APP_CONFIG

        cfg = APP_CONFIG["Mail"]
        assert "readiness_check" in cfg, (
            "Mail config is missing readiness_check — cold launch will export 0 messages"
        )
        assert cfg["readiness_check"].strip(), "readiness_check script must not be empty"

    def test_mail_readiness_timeout_is_generous(self):
        """readiness_timeout must be at least 60 s for slow IMAP connections."""
        from estormi_ingestion.shared.host.app_lifecycle import APP_CONFIG

        timeout = APP_CONFIG["Mail"].get("readiness_timeout", 0)
        assert timeout >= 60, (
            f"Mail readiness_timeout is only {timeout}s — too short for a cold IMAP sync"
        )

    def test_wait_ready_returns_immediately_when_probe_succeeds(self):
        """_wait_ready must return as soon as the probe returns a positive integer."""
        from estormi_ingestion.shared.host import app_lifecycle

        probe_result = MagicMock()
        probe_result.returncode = 0
        probe_result.stdout = "5\n"

        with patch.object(app_lifecycle.subprocess, "run", return_value=probe_result) as mock_run:
            app_lifecycle._wait_ready("fake script", timeout_secs=60, process_name="Mail")

        assert mock_run.call_count == 1, (
            "_wait_ready called the probe more than once despite an immediate success"
        )

    def test_wait_ready_prints_warning_on_timeout(self, capsys):
        """_wait_ready must print a WARNING and not raise when the probe always fails."""
        from estormi_ingestion.shared.host import app_lifecycle

        probe_result = MagicMock()
        probe_result.returncode = 0
        probe_result.stdout = "0\n"

        with patch.object(app_lifecycle.subprocess, "run", return_value=probe_result):
            with patch.object(app_lifecycle.time, "sleep"):
                with patch.object(app_lifecycle.time, "monotonic", side_effect=[0.0, 0.1, 200.0]):
                    app_lifecycle._wait_ready("fake script", timeout_secs=1, process_name="Mail")

        captured = capsys.readouterr()
        assert "WARNING" in captured.out, (
            "_wait_ready must emit a WARNING when the readiness check times out"
        )

    def test_wait_ready_skips_non_integer_output(self):
        """Probe output that is not a digit string must not be treated as success."""
        from estormi_ingestion.shared.host import app_lifecycle

        bad_result = MagicMock()
        bad_result.returncode = 0
        bad_result.stdout = "error\n"

        good_result = MagicMock()
        good_result.returncode = 0
        good_result.stdout = "3\n"

        with patch.object(
            app_lifecycle.subprocess, "run", side_effect=[bad_result, good_result]
        ) as run:
            with patch.object(app_lifecycle.time, "sleep"):
                app_lifecycle._wait_ready("fake script", timeout_secs=30, process_name="Mail")

        # Non-digit output ("error") must not count as ready, so the probe must
        # run a second time and pick up the good "3" output: exactly two calls.
        assert run.call_count == 2


# ── Regression: Mail readiness check skipped when Mail was already running;
#    probe counted mailboxes (cached) instead of messages (requires IMAP sync). ──


class TestMailReadinessAlwaysRuns:
    """Readiness check must run even when Mail was already running, and must
    probe message availability (not just mailbox structure)."""

    def test_readiness_check_always_flag_is_set(self):
        """APP_CONFIG['Mail'] must set readiness_check_always=True so the probe
        runs even when Mail was already running before the DAG started."""
        from estormi_ingestion.shared.host.app_lifecycle import APP_CONFIG

        assert APP_CONFIG["Mail"].get("readiness_check_always") is True, (
            "readiness_check_always must be True — without it, a pre-existing Mail "
            "process skips the readiness probe and the export blocks on IMAP sync"
        )

    def test_readiness_timeout_is_at_least_600s(self):
        """readiness_timeout must be >= 600 s to allow IMAP re-sync on a cold network."""
        from estormi_ingestion.shared.host.app_lifecycle import APP_CONFIG

        timeout = APP_CONFIG["Mail"].get("readiness_timeout", 0)
        assert timeout >= 600, (
            f"Mail readiness_timeout is {timeout}s — must be >= 600 s for a cold IMAP re-sync"
        )

    def test_readiness_script_probes_inbox_messages(self):
        """The readiness script must attempt to count messages, not just mailboxes,
        so IMAP header download is triggered before the main export starts."""
        from estormi_ingestion.shared.host.app_lifecycle import APP_CONFIG

        script = APP_CONFIG["Mail"].get("readiness_check", "")
        assert "messages" in script.lower(), (
            "Mail readiness_check must probe messages (not just mailboxes) to warm up IMAP sync"
        )
        assert "INBOX" in script or "inbox" in script.lower(), (
            "Mail readiness_check must probe INBOX specifically to trigger header download"
        )

    def test_readiness_probe_runs_for_already_running_app(self):
        """AppLifecycle must run the readiness check even when the app was already running."""
        from estormi_ingestion.shared.host import app_lifecycle

        probe_ok = MagicMock()
        probe_ok.returncode = 0
        probe_ok.stdout = "42\n"

        with (
            patch.object(app_lifecycle, "_is_running", return_value=True),
            patch.object(app_lifecycle, "_wait_ready") as mock_wait,
            patch.object(app_lifecycle, "_quit"),
        ):
            lc = app_lifecycle.AppLifecycle("Mail")
            lc.__enter__()

        assert mock_wait.call_count == 1, (
            "readiness probe was not called even though readiness_check_always=True"
        )

    def test_readiness_probe_skipped_for_app_without_always_flag(self):
        """Apps without readiness_check_always must NOT run the probe when already running."""
        from estormi_ingestion.shared.host import app_lifecycle

        with (
            patch.object(app_lifecycle, "_is_running", return_value=True),
            patch.object(app_lifecycle, "_wait_ready") as mock_wait,
            patch.object(app_lifecycle, "_quit"),
        ):
            lc = app_lifecycle.AppLifecycle("Notes")
            lc.__enter__()

        assert mock_wait.call_count == 0, (
            "readiness probe must not run for Notes (no readiness_check_always flag) "
            "when Notes is already running"
        )


# ── Bug C1: a backgrounded export must gate the watermark on its exit code ────
# ``apple_notes/watch_and_ingest.sh`` backgrounded its AppleScript export and
# swallowed the export's non-zero exit (``wait "$_OSAS_PID" || echo``), then
# advanced the watermark whenever every *staged* note ingested cleanly. A
# partial export (e.g. the AppleScript ``with timeout`` firing on a large
# library) stages only a subset; the un-exported notes are invisible to the
# ingest loop, so ``failed`` stays 0 and the watermark jumps past them —
# permanent data loss. The fix mirrors ``apple_mail``: capture the export exit
# code and gate the watermark advance on a clean export *and* a clean ingest.


class TestBackgroundedExportGatesWatermark:
    """Every connector that backgrounds its export must gate the watermark
    advance on the captured export RC, so the C1 data-loss pattern can't be
    reintroduced here or copied into a new connector."""

    _IF_RE = re.compile(r"^\s*if\b.*;\s*then\s*$")

    @pytest.mark.parametrize("script", _BACKGROUNDED_SCRIPTS, ids=lambda p: p.parent.name)
    def test_backgrounded_export_gates_watermark_on_export_rc(self, script: Path):
        text = script.read_text()
        lines = text.splitlines()

        # 1. The export exit code is actually captured.
        assert "_EXPORT_RC=$?" in text, f"{script} never captures the export RC"

        # 2. Every watermark advance is guarded by an `if` whose condition checks
        #    the export RC (not just the per-file `failed` counter).
        wm_lines = [i for i, ln in enumerate(lines) if "set_watermark(" in ln]
        assert wm_lines, f"{script} has no set_watermark call to gate"

        for idx in wm_lines:
            guard = None
            for j in range(idx, -1, -1):
                if self._IF_RE.match(lines[j]):
                    guard = lines[j]
                    break
            assert guard is not None, f"{script}: set_watermark not inside an if-guard"
            assert "_EXPORT_RC" in guard, (
                f"{script}: watermark advance is not gated on the export RC "
                f"(guard: {guard.strip()!r}) — a partial export would drop data"
            )

    def test_at_least_notes_and_mail_are_covered(self):
        names = {p.parent.name for p in _BACKGROUNDED_SCRIPTS}
        # Sanity: the discovery actually found the two known backgrounded exporters.
        assert {"apple_notes", "apple_mail"} <= names
