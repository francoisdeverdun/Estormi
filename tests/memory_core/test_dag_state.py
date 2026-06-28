"""Tests for memory_core/dag_state.py — DB-backed DAG run/stage state.

Covers the lifecycle (start/finish run + stage), concurrent stages, recent-run
ordering, orphan reconciliation, finish idempotency, the CLI, the shell
integration, and the defensive ALTER-TABLE schema migration that adds the
``engine`` column to a pre-existing ``dag_runs`` table (legacy installs) before
the DDL's ``CREATE INDEX dag_runs_engine_idx`` runs — without it the index DDL
crashes with "no such column: engine".
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from memory_core import dag_state
from tests.helpers.database import apply_runtime_schema_sync

pytestmark = pytest.mark.unit

# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture
def dag_db(tmp_path, monkeypatch):
    db_path = str(tmp_path / "estormi-dag-state.db")
    apply_runtime_schema_sync(db_path)
    monkeypatch.setattr(dag_state, "DB_PATH_OVERRIDE", db_path)
    return db_path


# ── Lifecycle: start/finish run + stage ──────────────────────────────────────


class TestRunLifecycle:
    def test_start_run_returns_int_id(self, dag_db):
        run_id = dag_state.start_run("manual", "/tmp/dag.log", "/tmp/dag.err")
        assert isinstance(run_id, int)
        assert run_id >= 1

    def test_start_run_initially_running(self, dag_db):
        run_id = dag_state.start_run("scheduled", "/tmp/a.log", "/tmp/a.err")
        run = dag_state.get_run(run_id)
        assert run is not None
        assert run.status == "running"
        assert run.ended_at is None
        assert run.trigger == "scheduled"
        assert run.log_path == "/tmp/a.log"
        assert run.err_path == "/tmp/a.err"

    def test_finish_run_records_status_and_duration(self, dag_db):
        run_id = dag_state.start_run("manual", "/tmp/a.log", "/tmp/a.err")
        dag_state.finish_run(run_id, "ok", duration_ms=12_345)
        run = dag_state.get_run(run_id)
        assert run is not None
        assert run.status == "ok"
        assert run.duration_ms == 12_345
        assert run.ended_at is not None

    def test_finish_run_computes_duration_when_omitted(self, dag_db):
        # Hand-craft a started_at well in the past so the computed duration
        # is deterministic relative to now (≥ 1s).
        from memory_core import dag_state as ds

        rid = ds.start_run(
            "manual",
            "/tmp/a.log",
            "/tmp/a.err",
            started_at="2024-01-01T00:00:00+00:00",
        )
        ds.finish_run(rid, "ok", ended_at="2024-01-01T00:01:00+00:00")
        run = ds.get_run(rid)
        assert run.duration_ms == 60_000


class TestStageLifecycle:
    def test_start_stage_attaches_to_run(self, dag_db):
        run_id = dag_state.start_run("manual", "/tmp/a.log", "/tmp/a.err")
        stage_id = dag_state.start_stage(run_id, "notes", "/tmp/notes.log")
        assert stage_id >= 1
        run = dag_state.get_run(run_id)
        assert len(run.stages) == 1
        stage = run.stages[0]
        assert stage.stage_name == "notes"
        assert stage.status == "running"
        assert stage.log_path == "/tmp/notes.log"

    def test_finish_stage_records_metrics(self, dag_db):
        run_id = dag_state.start_run("manual", "/tmp/a.log", "/tmp/a.err")
        stage_id = dag_state.start_stage(run_id, "mail", "/tmp/mail.log")
        dag_state.finish_stage(
            stage_id,
            "ok",
            exit_code=0,
            duration_ms=5_000,
        )
        run = dag_state.get_run(run_id)
        s = run.stages[0]
        assert s.status == "ok"
        assert s.exit_code == 0
        assert s.duration_ms == 5_000

    def test_failed_stage(self, dag_db):
        run_id = dag_state.start_run("manual", "/tmp/a.log", "/tmp/a.err")
        stage_id = dag_state.start_stage(run_id, "documents", None)
        dag_state.finish_stage(
            stage_id,
            "failed",
            exit_code=2,
            stderr_excerpt="some traceback",
        )
        run = dag_state.get_run(run_id)
        s = run.stages[0]
        assert s.status == "failed"
        assert s.stderr_excerpt == "some traceback"


# ── Concurrent stages within a single run ────────────────────────────────────


class TestConcurrentStages:
    def test_multiple_stages_in_one_run(self, dag_db):
        run_id = dag_state.start_run("manual", "/tmp/a.log", "/tmp/a.err")
        ids = [
            dag_state.start_stage(run_id, name, f"/tmp/{name}.log")
            for name in ("notes", "mail", "calendar")
        ]
        for sid in ids:
            dag_state.finish_stage(sid, "ok")
        run = dag_state.get_run(run_id)
        assert {s.stage_name for s in run.stages} == {"notes", "mail", "calendar"}
        assert all(s.status == "ok" for s in run.stages)


# ── get_recent_runs ordering + limit ─────────────────────────────────────────


class TestRecentRuns:
    def test_returns_newest_first(self, dag_db):
        # Use raw inserts to control started_at ordering deterministically.
        conn = sqlite3.connect(dag_db)
        for ts in ("2024-01-01T00:00:00+00:00", "2024-02-01T00:00:00+00:00"):
            conn.execute(
                "INSERT INTO dag_runs (started_at, status, trigger) VALUES (?, 'ok', 'manual')",
                (ts,),
            )
        conn.commit()
        conn.close()
        runs = dag_state.get_recent_runs()
        assert runs[0].started_at.month == 2
        assert runs[1].started_at.month == 1

    def test_respects_limit(self, dag_db):
        conn = sqlite3.connect(dag_db)
        for i in range(5):
            conn.execute(
                "INSERT INTO dag_runs (started_at, status, trigger) VALUES (?, 'ok', 'manual')",
                (f"2024-0{i + 1}-01T00:00:00+00:00",),
            )
        conn.commit()
        conn.close()
        runs = dag_state.get_recent_runs(limit=3)
        assert len(runs) == 3

    def test_empty_db_returns_empty(self, dag_db):
        assert dag_state.get_recent_runs() == []


# ── reconcile_orphaned_runs ──────────────────────────────────────────────────


class TestReconcile:
    def test_marks_running_runs_cancelled_when_no_pid(self, dag_db):
        rid = dag_state.start_run("manual", "/tmp/a.log", "/tmp/a.err")
        sid = dag_state.start_stage(rid, "notes", "/tmp/notes.log")
        dag_state.reconcile_orphaned_runs(pid_file_exists=False)
        run = dag_state.get_run(rid)
        assert run.status == "cancelled"
        assert run.ended_at is not None
        assert run.stages[0].id == sid
        # Orphaned stages are ``cancelled`` (preempted), not ``failed`` — the
        # connector never reported a failure, the process was killed.
        assert run.stages[0].status == "cancelled"

    def test_noop_when_pid_file_exists(self, dag_db):
        rid = dag_state.start_run("manual", "/tmp/a.log", "/tmp/a.err")
        dag_state.reconcile_orphaned_runs(pid_file_exists=True)
        run = dag_state.get_run(rid)
        assert run.status == "running"

    def test_only_targets_requested_engine(self, dag_db):
        """A reconcile pass for one engine must not cancel another's live runs."""
        ingest_run = dag_state.start_run(
            "scheduled", "/tmp/i.log", "/tmp/i.err", engine="ingestion"
        )
        brief_run = dag_state.start_run("scheduled", "/tmp/c.log", "/tmp/c.err", engine="briefing")
        # Ingestion pidfile is absent → only ingestion rows should be cancelled.
        dag_state.reconcile_orphaned_runs(pid_file_exists=False, engine="ingestion")
        assert dag_state.get_run(ingest_run).status == "cancelled"
        assert dag_state.get_run(brief_run).status == "running"


class TestFinishIdempotency:
    """A late SIGTERM trap must not flip an already-ok row to cancelled/failed."""

    def test_finish_run_does_not_overwrite_ok(self, dag_db):
        rid = dag_state.start_run("manual", "/tmp/a.log", "/tmp/a.err")
        dag_state.finish_run(rid, "ok", duration_ms=100)
        # Second call (e.g. from a late trap) must not change anything.
        dag_state.finish_run(rid, "cancelled", duration_ms=999)
        run = dag_state.get_run(rid)
        assert run.status == "ok"
        assert run.duration_ms == 100

    def test_finish_stage_does_not_overwrite_ok(self, dag_db):
        rid = dag_state.start_run("manual", "/tmp/a.log", "/tmp/a.err")
        sid = dag_state.start_stage(rid, "notes", "/tmp/notes.log")
        dag_state.finish_stage(sid, "ok", exit_code=0, duration_ms=50)
        dag_state.finish_stage(sid, "failed", exit_code=143, duration_ms=999)
        run = dag_state.get_run(rid)
        assert run.stages[0].status == "ok"
        assert run.stages[0].exit_code == 0
        assert run.stages[0].duration_ms == 50


# ── CLI ──────────────────────────────────────────────────────────────────────


class TestCli:
    def _cli(self, db_path: str, *args: str) -> str:
        env = {
            **os.environ,
            "ESTORMI_DB_PATH": db_path,
        }
        repo_root = Path(__file__).resolve().parent.parent.parent
        env["PYTHONPATH"] = str(repo_root) + os.pathsep + env.get("PYTHONPATH", "")
        result = subprocess.run(
            [sys.executable, "-m", "memory_core.dag_state", *args],
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )
        assert result.returncode == 0, f"CLI failed: {result.stderr}"
        return result.stdout.strip()

    @pytest.mark.skipif(shutil.which("bash") is None, reason="bash not available")
    def test_full_lifecycle_through_cli(self, tmp_path):
        db_path = str(tmp_path / "cli-test.db")
        apply_runtime_schema_sync(db_path)

        run_id = self._cli(
            db_path,
            "start-run",
            "--trigger",
            "manual",
            "--log-path",
            "/tmp/dag.log",
            "--err-path",
            "/tmp/dag.err",
        )
        assert run_id.isdigit()

        stage_id = self._cli(
            db_path,
            "start-stage",
            "--run-id",
            run_id,
            "--stage",
            "notes",
            "--log-path",
            "/tmp/notes.log",
        )
        assert stage_id.isdigit()

        self._cli(
            db_path,
            "finish-stage",
            "--stage-id",
            stage_id,
            "--status",
            "ok",
            "--exit-code",
            "0",
            "--duration-ms",
            "1500",
        )
        self._cli(
            db_path,
            "finish-run",
            "--run-id",
            run_id,
            "--status",
            "ok",
            "--duration-ms",
            "2500",
        )

        # Verify with raw SQL — the CLI is the only writer.
        conn = sqlite3.connect(db_path)
        try:
            r = conn.execute(
                "SELECT status, duration_ms FROM dag_runs WHERE id = ?", (run_id,)
            ).fetchone()
            assert r == ("ok", 2500)
            s = conn.execute(
                "SELECT status, exit_code, duration_ms FROM dag_stages WHERE id = ?",
                (stage_id,),
            ).fetchone()
            assert s == ("ok", 0, 1500)
        finally:
            conn.close()

    @pytest.mark.skipif(shutil.which("bash") is None, reason="bash not available")
    def test_start_run_engine_flag_persists_to_db(self, tmp_path):
        """CLI's ``--engine`` must reach ``dag_runs.engine`` — without it every
        shell-launched run would be labelled ``ingestion`` regardless of which
        engine actually ran."""
        db_path = str(tmp_path / "engine-flag.db")
        apply_runtime_schema_sync(db_path)

        run_id = self._cli(
            db_path,
            "start-run",
            "--trigger",
            "manual",
            "--engine",
            "briefing",
            "--log-path",
            "/tmp/c.log",
            "--err-path",
            "/tmp/c.err",
        )

        conn = sqlite3.connect(db_path)
        try:
            row = conn.execute(
                "SELECT engine FROM dag_runs WHERE id = ?", (int(run_id),)
            ).fetchone()
            assert row == ("briefing",)
        finally:
            conn.close()


# ── End-to-end: shell script → CLI → SQLite ──────────────────────────────────


class TestShellIntegration:
    """Drives the ``memory_core.dag_state`` CLI from an inline bash harness.

    Mirrors how ``scripts/daily_ingestion.sh`` calls
    ``start-run`` / ``start-stage`` / ``finish-stage`` / ``finish-run``, and
    asserts the run/stage rows land in SQLite. It exercises the CLI directly;
    it does not source ``daily_ingestion.sh``.
    """

    @pytest.mark.skipif(shutil.which("bash") is None, reason="bash not available")
    def test_shell_records_run_to_sqlite(self, tmp_path):
        db_path = str(tmp_path / "shell-test.db")
        apply_runtime_schema_sync(db_path)
        repo_root = Path(__file__).resolve().parent.parent.parent

        script = f"""
        set -euo pipefail
        export ESTORMI_DB_PATH="{db_path}"
        export PYTHONPATH="{repo_root}:${{PYTHONPATH:-}}"
        PY="{sys.executable}"
        RUN_ID="$($PY -m memory_core.dag_state start-run \\
            --trigger manual --log-path /tmp/dag.log --err-path /tmp/dag.err)"
        STAGE_ID="$($PY -m memory_core.dag_state start-stage \\
            --run-id "$RUN_ID" --stage notes --log-path /tmp/notes.log)"
        $PY -m memory_core.dag_state finish-stage \\
            --stage-id "$STAGE_ID" --status ok --exit-code 0 --duration-ms 1000
        $PY -m memory_core.dag_state finish-run \\
            --run-id "$RUN_ID" --status ok --duration-ms 1500
        echo "$RUN_ID $STAGE_ID"
        """

        result = subprocess.run(["bash", "-c", script], capture_output=True, text=True, check=False)
        assert result.returncode == 0, f"shell harness failed: {result.stderr}"
        run_id, stage_id = result.stdout.strip().split()

        conn = sqlite3.connect(db_path)
        try:
            assert conn.execute(
                "SELECT status FROM dag_runs WHERE id = ?", (int(run_id),)
            ).fetchone() == ("ok",)
            assert conn.execute(
                "SELECT status FROM dag_stages WHERE id = ?", (int(stage_id),)
            ).fetchone() == ("ok",)
        finally:
            conn.close()


# ── Legacy schema migration ──────────────────────────────────────────────────

# A legacy ``dag_runs`` table as it shipped before the ``engine`` column.
_LEGACY_DAG_RUNS = """
CREATE TABLE dag_runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at  TEXT NOT NULL,
    ended_at    TEXT,
    status      TEXT NOT NULL DEFAULT 'running',
    duration_ms INTEGER,
    trigger     TEXT,
    log_path    TEXT,
    err_path    TEXT,
    notes       TEXT
);
"""


def test_ensure_schema_sync_migrates_legacy_dag_runs(tmp_path):
    from memory_core.dag_state import _ensure_schema_sync

    db = tmp_path / "legacy.db"
    conn = sqlite3.connect(db)
    try:
        conn.executescript(_LEGACY_DAG_RUNS)
        conn.commit()
        # Sanity: the legacy table has no ``engine`` column.
        cols = {row[1] for row in conn.execute("PRAGMA table_info(dag_runs)")}
        assert "engine" not in cols

        _ensure_schema_sync(conn)

        cols = {row[1] for row in conn.execute("PRAGMA table_info(dag_runs)")}
        assert "engine" in cols

        indexes = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='dag_runs'"
            )
        }
        assert "dag_runs_engine_idx" in indexes
    finally:
        conn.close()
