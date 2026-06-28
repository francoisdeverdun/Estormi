"""Tests for pipeline.py pure functions and data models."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from estormi_server.services.pipeline_status import (
    DAG_STAGES,
    DagRun,
    StageRun,
    _fmt_duration,
    _next_run_at,
    _parse_dag_log,
    _time_ago,
    set_schedule_cron,
)
from memory_core import dag_state
from tests.helpers.database import apply_runtime_schema_sync

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def _restore_schedule_cron():
    """Snapshot ``pipeline._schedule_cron`` and restore it after every test.

    Several tests below call ``set_schedule_cron(...)`` to exercise the
    scheduling math; without this fixture the mutation leaks into whatever
    test happens to run next, producing order-dependent failures.
    """
    import estormi_server.services.pipeline_status as _pipeline

    original = _pipeline._schedule_cron
    yield
    _pipeline._schedule_cron = original


@pytest.fixture
def dag_db(tmp_path, monkeypatch):
    """Provide a fresh, schema-applied file-backed SQLite DB pointed-to by dag_state.

    Returns the DB path. Tests use raw sqlite3 to seed ``dag_runs`` / ``dag_stages``
    so they exercise the same read path the production CLI writes through.
    """
    db_path = str(tmp_path / "estormi-test.db")
    apply_runtime_schema_sync(db_path)
    monkeypatch.setattr(dag_state, "DB_PATH_OVERRIDE", db_path)
    return db_path


def _insert_run(
    db_path: str,
    started_at: str,
    ended_at: str | None = None,
    status: str = "ok",
    duration_ms: int | None = None,
    log_path: str = "",
    err_path: str = "",
    trigger: str = "scheduled",
    engine: str = "ingestion",
) -> int:
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.execute(
            "INSERT INTO dag_runs (started_at, ended_at, status, duration_ms, "
            "trigger, log_path, err_path, engine) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (started_at, ended_at, status, duration_ms, trigger, log_path, err_path, engine),
        )
        conn.commit()
        return int(cur.lastrowid)
    finally:
        conn.close()


def _insert_stage(
    db_path: str,
    run_id: int,
    stage_name: str,
    started_at: str,
    ended_at: str | None = None,
    status: str = "ok",
    duration_ms: int | None = None,
    log_path: str | None = None,
    exit_code: int | None = None,
) -> int:
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.execute(
            "INSERT INTO dag_stages (run_id, stage_name, started_at, ended_at, "
            "status, duration_ms, log_path, exit_code) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                run_id,
                stage_name,
                started_at,
                ended_at,
                status,
                duration_ms,
                log_path,
                exit_code,
            ),
        )
        conn.commit()
        return int(cur.lastrowid)
    finally:
        conn.close()


# ── _fmt_duration ─────────────────────────────────────────────────────────────


class TestFmtDuration:
    def test_none(self):
        assert _fmt_duration(None) == "—"

    def test_seconds(self):
        assert _fmt_duration(45) == "45s"

    def test_minutes(self):
        assert _fmt_duration(125) == "2m 5s"

    def test_hours(self):
        assert _fmt_duration(3661) == "1h 1m"


# ── _time_ago ─────────────────────────────────────────────────────────────────


class TestTimeAgo:
    def test_none(self):
        assert _time_ago(None) == "never"

    def test_seconds(self):
        dt = datetime.now(timezone.utc) - timedelta(seconds=30)
        assert "s ago" in _time_ago(dt)

    def test_minutes(self):
        dt = datetime.now(timezone.utc) - timedelta(minutes=5)
        assert "m ago" in _time_ago(dt)

    def test_hours(self):
        dt = datetime.now(timezone.utc) - timedelta(hours=3)
        assert "h ago" in _time_ago(dt)

    def test_days(self):
        dt = datetime.now(timezone.utc) - timedelta(days=2)
        assert "d ago" in _time_ago(dt)

    def test_naive_dt_treated_as_utc(self):
        dt = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=5)
        result = _time_ago(dt)
        assert "ago" in result


# ── StageRun ──────────────────────────────────────────────────────────────────


class TestStageRun:
    def test_defaults(self):
        s = StageRun(name="notes", status="ok")
        assert s.duration_s is None
        assert s.offset_s is None
        assert s.log_file is None

    def test_full(self):
        s = StageRun(
            name="mail",
            status="fail",
            duration_s=30,
            offset_s=10,
            log_file="/tmp/a.log",
        )
        assert s.name == "mail"
        assert s.status == "fail"


# ── DagRun ────────────────────────────────────────────────────────────────────


class TestDagRun:
    def _make_run(self, **kwargs):
        defaults = dict(started_at=datetime(2024, 6, 15, 2, 0, 0, tzinfo=timezone.utc))
        defaults.update(kwargs)
        return DagRun(**defaults)

    def test_ended_at_none_when_running(self):
        run = self._make_run()
        assert run.ended_at is None

    def test_ended_at_computed(self):
        run = self._make_run(total_s=600, is_running=False)
        assert run.ended_at == run.started_at + timedelta(seconds=600)

    def test_failed_stages(self):
        run = self._make_run(
            stages=[
                StageRun(name="notes", status="ok"),
                StageRun(name="mail", status="fail"),
            ]
        )
        assert run.failed_stages == ["mail"]

    def test_no_failed_stages(self):
        run = self._make_run(stages=[StageRun(name="notes", status="ok")])
        assert run.failed_stages == []

    def test_overall_status_running(self):
        run = self._make_run(is_running=True)
        assert run.overall_status == "running"

    def test_overall_status_ok(self):
        run = self._make_run(is_running=False, stages=[StageRun(name="notes", status="ok")])
        assert run.overall_status == "ok"

    def test_overall_status_fail(self):
        run = self._make_run(
            is_running=False,
            stages=[StageRun(name="mail", status="fail")],
        )
        assert run.overall_status == "fail"

    def test_enriched_stages_all_done(self):
        stages = [StageRun(name=n, status="ok", duration_s=10) for n in DAG_STAGES]
        run = self._make_run(stages=stages, is_running=False)
        enriched = run.enriched_stages()
        assert len(enriched) == len(DAG_STAGES)
        assert all(s.status == "ok" for s in enriched)

    def test_enriched_stages_partial(self):
        # Only first 3 stages done, no stage_starts logged for the rest
        stages = [StageRun(name=n, status="ok") for n in DAG_STAGES[:3]]
        run = self._make_run(stages=stages, is_running=True)
        enriched = run.enriched_stages()
        assert len(enriched) == len(DAG_STAGES)
        # Sequential DAG: stages not yet started are "pending" (no stage_starts entry)
        for s in enriched[3:]:
            assert s.status == "pending"


# ── _parse_dag_log (reads from dag_runs/dag_stages) ───────────────────────────


class TestParseDagLog:
    def test_no_runs(self, dag_db):
        """Empty DB → empty list."""
        assert _parse_dag_log() == []

    def test_full_run(self, dag_db):
        """Reconstruct stages + duration from dag_runs/dag_stages."""
        run_id = _insert_run(
            dag_db,
            started_at="2024-06-15T02:00:00+00:00",
            ended_at="2024-06-15T02:00:30+00:00",
            status="ok",
            duration_ms=30_000,
            log_path="/tmp/dag-20240615.log",
        )
        _insert_stage(
            dag_db,
            run_id,
            "notes",
            "2024-06-15T02:00:05+00:00",
            "2024-06-15T02:00:15+00:00",
            status="ok",
            duration_ms=10_000,
        )
        _insert_stage(
            dag_db,
            run_id,
            "mail",
            "2024-06-15T02:00:15+00:00",
            "2024-06-15T02:00:20+00:00",
            status="failed",
            duration_ms=5_000,
        )

        runs = _parse_dag_log()
        assert len(runs) == 1
        run = runs[0]
        assert run.total_s == 30
        assert not run.is_running
        assert len(run.stages) == 2
        assert run.stages[0].name == "notes"
        assert run.stages[0].status == "ok"
        assert run.stages[0].duration_s == 10
        assert run.stages[1].name == "mail"
        assert run.stages[1].status == "fail"

    def test_running_dag_no_pid(self, dag_db, monkeypatch):
        """A run still status=running but with no live process is treated as crashed."""
        _insert_run(
            dag_db,
            started_at="2024-06-15T02:00:00+00:00",
            status="running",
            log_path="/tmp/dag-20240615.log",
        )
        # No live daily_ingestion.sh process — liveness is probed via the
        # process table, not the (unreliable) pid file.
        import estormi_server.services.pipeline_status as _pipeline

        monkeypatch.setattr(_pipeline, "_dag_process_alive", lambda: False)
        runs = _parse_dag_log()
        assert len(runs) == 1
        assert not runs[0].is_running

    def test_multiple_runs(self, dag_db):
        """Multiple DAG rows are returned oldest → newest."""
        _insert_run(
            dag_db,
            started_at="2024-06-14T02:00:00+00:00",
            ended_at="2024-06-14T02:00:15+00:00",
            status="ok",
            duration_ms=15_000,
        )
        _insert_run(
            dag_db,
            started_at="2024-06-15T02:00:00+00:00",
            ended_at="2024-06-15T02:00:20+00:00",
            status="ok",
            duration_ms=20_000,
        )
        runs = _parse_dag_log()
        assert len(runs) == 2
        # Oldest first: 2024-06-14 then 2024-06-15
        assert runs[0].started_at.day == 14
        assert runs[1].started_at.day == 15

    def test_ignores_briefing_runs(self, dag_db):
        """Only ingestion runs feed the pipeline view.

        Regression: briefing runs share the dag_runs table but carry no DAG
        stages. An unfiltered query let the newest briefing land as runs[-1],
        so ``last`` was a stage-less run that rendered every stage "pending"
        and reported the briefing's timing as the pipeline's last run.
        """
        ingest_id = _insert_run(
            dag_db,
            started_at="2024-06-15T02:00:00+00:00",
            ended_at="2024-06-15T02:04:25+00:00",
            status="ok",
            duration_ms=265_000,
            engine="ingestion",
        )
        _insert_stage(
            dag_db,
            ingest_id,
            "notes",
            "2024-06-15T02:00:05+00:00",
            "2024-06-15T02:00:15+00:00",
            status="ok",
            duration_ms=10_000,
        )
        # A briefing run inserted AFTER the ingestion run — higher id, so it
        # would be runs[-1] without the engine filter.
        _insert_run(
            dag_db,
            started_at="2024-06-15T02:35:00+00:00",
            ended_at="2024-06-15T02:39:42+00:00",
            status="ok",
            duration_ms=282_000,
            engine="briefing",
        )

        runs = _parse_dag_log()
        assert len(runs) == 1
        last = runs[-1]
        assert last.total_s == 265  # ingestion's duration, not the briefing's
        assert [s.name for s in last.stages] == ["notes"]
        assert last.enriched_stages()[0].status == "ok"  # not "pending"

    def test_stage_log_captured(self, dag_db):
        """Stage log_path round-trips through the dag_stages table."""
        run_id = _insert_run(
            dag_db,
            started_at="2024-06-15T02:00:00+00:00",
            ended_at="2024-06-15T02:00:12+00:00",
            status="ok",
            duration_ms=12_000,
        )
        _insert_stage(
            dag_db,
            run_id,
            "notes",
            "2024-06-15T02:00:00+00:00",
            "2024-06-15T02:00:10+00:00",
            status="ok",
            duration_ms=10_000,
            log_path="/tmp/notes.log",
        )
        runs = _parse_dag_log()
        assert runs[0].stages[0].log_file == "/tmp/notes.log"


# ── _next_run_at / set_schedule_cron ─────────────────────────────────────────


class TestNextRunAt:
    def test_default_uses_module_schedule(self):
        """_next_run_at() with no args uses the module-level _schedule_cron."""

        set_schedule_cron("0 2 * * *")
        dt = _next_run_at()
        assert dt is not None
        assert dt.hour == 2 and dt.minute == 0

    def test_reflects_updated_schedule(self):
        """After set_schedule_cron, _next_run_at() reflects the new time."""

        set_schedule_cron("0 11 * * *")
        dt = _next_run_at()
        assert dt is not None
        assert dt.hour == 11 and dt.minute == 0

    def test_manual_returns_none(self):
        set_schedule_cron("manual")
        assert _next_run_at() is None

    def test_get_pipeline_data_next_run_matches_schedule(self, dag_db):
        """get_pipeline_data()['next_run_at'] reflects the active cron, not a hardcoded 02:00."""
        set_schedule_cron("0 11 * * *")
        from estormi_server.services.pipeline_status import get_pipeline_data

        data = get_pipeline_data()
        assert data["next_run_at"] is not None
        assert "11:00" in data["next_run_at"], (
            f"next_run_at should reflect 11:00 schedule, got: {data['next_run_at']}"
        )


# ── _aggregate_source_run_logs ───────────────────────────────────────────────


class TestAggregateSourceRunLogs:
    """The per-source live tail merges the cumulative ``source-<name>.log``
    (manual runs) with the per-DAG-run ``estormi-stage-*-<name>.log`` files,
    ordered by mtime. Regression: it used to short-circuit to the cumulative
    file whenever it existed, freezing the modal on a stale manual run once
    later cron runs landed."""

    def _write(self, path, body, mtime):
        import os

        path.write_text(body, encoding="utf-8")
        os.utime(path, (mtime, mtime))

    def test_merges_cumulative_and_per_run_newest_last(self, tmp_path):
        from estormi_server.api.pipeline import _aggregate_source_run_logs

        # Stale manual run (older), then a fresh cron run (newer).
        self._write(tmp_path / "source-knowledge.log", "OLD MANUAL LINE", mtime=1_000)
        self._write(
            tmp_path / "estormi-stage-20260601-082437-knowledge.log",
            "NEW CRON LINE",
            mtime=2_000,
        )

        out = _aggregate_source_run_logs("knowledge", str(tmp_path), lines=2000)

        # Both runs present, each under its own header.
        assert "OLD MANUAL LINE" in out
        assert "NEW CRON LINE" in out
        assert "── run manual runs ──" in out
        assert "── run 20260601-082437 ──" in out
        # Newest activity lands at the tail (chronological by mtime).
        assert out.index("NEW CRON LINE") > out.index("OLD MANUAL LINE")

    def test_per_run_only_when_no_cumulative(self, tmp_path):
        from estormi_server.api.pipeline import _aggregate_source_run_logs

        self._write(
            tmp_path / "estormi-stage-20260601-082437-knowledge.log",
            "CRON ONLY",
            mtime=2_000,
        )
        out = _aggregate_source_run_logs("knowledge", str(tmp_path), lines=2000)
        assert "CRON ONLY" in out

    def test_empty_when_no_logs(self, tmp_path):
        from estormi_server.api.pipeline import _aggregate_source_run_logs

        assert _aggregate_source_run_logs("knowledge", str(tmp_path), lines=2000) == ""


class TestRecentErrors:
    """Bug S3: ``_recent_errors`` tails the DAG error log via
    ``server.log_tail.tail_lines`` instead of slurping the whole file with
    ``Path.read_text`` (which loaded multi-megabyte logs into memory every poll)."""

    @staticmethod
    def _make_log(path, n_lines: int) -> list[str]:
        lines = [f"error line {i}" for i in range(n_lines)]
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return lines

    def test_recent_errors_returns_last_20_lines(self, tmp_path, monkeypatch):
        from estormi_server.services import pipeline_status as pipeline

        err_log = tmp_path / "estormi-daily-dag-error.log"
        all_lines = self._make_log(err_log, 5000)
        expected = all_lines[-20:]

        monkeypatch.setattr(pipeline, "DAG_ERR_LOG_CANDIDATES", [err_log])

        result = pipeline._recent_errors()

        assert result == expected, f"Expected last 20 lines, got: {result[:3]}…"

    def test_recent_errors_does_not_call_read_text(self, tmp_path, monkeypatch):
        from pathlib import Path

        from estormi_server.services import pipeline_status as pipeline

        err_log = tmp_path / "estormi-daily-dag-error.log"
        self._make_log(err_log, 5000)

        monkeypatch.setattr(pipeline, "DAG_ERR_LOG_CANDIDATES", [err_log])

        read_text_called: list[str] = []
        original_read_text = Path.read_text

        def spy_read_text(self, *args, **kwargs):
            if self == err_log:
                read_text_called.append(str(self))
            return original_read_text(self, *args, **kwargs)

        monkeypatch.setattr(Path, "read_text", spy_read_text)
        pipeline._recent_errors()

        assert read_text_called == [], (
            f"Path.read_text was called on the log file: {read_text_called}. "
            "Fix did not take effect — tail_lines must be used instead."
        )

    def test_recent_errors_missing_file_returns_empty(self, tmp_path, monkeypatch):
        from estormi_server.services import pipeline_status as pipeline

        missing = tmp_path / "no-such-file.log"
        monkeypatch.setattr(pipeline, "DAG_ERR_LOG_CANDIDATES", [missing])

        assert pipeline._recent_errors() == []

    def test_recent_errors_skips_blank_lines(self, tmp_path, monkeypatch):
        from estormi_server.services import pipeline_status as pipeline

        err_log = tmp_path / "estormi-daily-dag-error.log"
        lines = [f"error {i}" if i % 2 == 0 else "" for i in range(25)]
        err_log.write_text("\n".join(lines) + "\n", encoding="utf-8")

        monkeypatch.setattr(pipeline, "DAG_ERR_LOG_CANDIDATES", [err_log])

        result = pipeline._recent_errors()
        assert all(ln.strip() for ln in result), "Blank lines should be filtered out"

    def test_recent_errors_uses_tail_lines(self, tmp_path, monkeypatch):
        from estormi_server.services import pipeline_status as pipeline

        err_log = tmp_path / "estormi-daily-dag-error.log"
        self._make_log(err_log, 100)

        monkeypatch.setattr(pipeline, "DAG_ERR_LOG_CANDIDATES", [err_log])

        calls: list[tuple] = []
        original_tail = pipeline.tail_lines

        def spy_tail(path, n_lines, *args, **kwargs):
            calls.append((path, n_lines))
            return original_tail(path, n_lines, *args, **kwargs)

        # tail_lines is imported directly into pipeline's namespace; patch there.
        monkeypatch.setattr(pipeline, "tail_lines", spy_tail)

        pipeline._recent_errors()

        assert len(calls) == 1, f"tail_lines should be called once, got: {calls}"
        _, n = calls[0]
        assert n == 20, f"tail_lines should request 20 lines, got: {n}"
