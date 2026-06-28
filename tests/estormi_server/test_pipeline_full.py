"""Tests for pipeline.py — DB-backed parsing and data functions."""

from __future__ import annotations

import sqlite3

import pytest

from estormi_server.services.pipeline_status import (
    _recent_errors,
    get_pipeline_data,
)
from memory_core import dag_state
from tests.helpers.database import apply_runtime_schema_sync

pytestmark = pytest.mark.integration


@pytest.fixture
def dag_db(tmp_path, monkeypatch):
    db_path = str(tmp_path / "estormi-test.db")
    apply_runtime_schema_sync(db_path)
    monkeypatch.setattr(dag_state, "DB_PATH_OVERRIDE", db_path)
    return db_path


def _insert_run(db_path: str, **kwargs) -> int:
    fields = {
        "started_at": "2024-06-15T02:00:00+00:00",
        "ended_at": None,
        "status": "running",
        "duration_ms": None,
        "trigger": "scheduled",
        "log_path": "",
        "err_path": "",
    }
    fields.update(kwargs)
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.execute(
            "INSERT INTO dag_runs (started_at, ended_at, status, duration_ms, "
            "trigger, log_path, err_path) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                fields["started_at"],
                fields["ended_at"],
                fields["status"],
                fields["duration_ms"],
                fields["trigger"],
                fields["log_path"],
                fields["err_path"],
            ),
        )
        conn.commit()
        return int(cur.lastrowid)
    finally:
        conn.close()


def _insert_stage(db_path: str, run_id: int, stage_name: str, **kwargs) -> int:
    fields = {
        "started_at": "2024-06-15T02:00:00+00:00",
        "ended_at": None,
        "status": "running",
        "duration_ms": None,
        "log_path": None,
        "exit_code": None,
    }
    fields.update(kwargs)
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.execute(
            "INSERT INTO dag_stages (run_id, stage_name, started_at, ended_at, "
            "status, duration_ms, log_path, exit_code) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                run_id,
                stage_name,
                fields["started_at"],
                fields["ended_at"],
                fields["status"],
                fields["duration_ms"],
                fields["log_path"],
                fields["exit_code"],
            ),
        )
        conn.commit()
        return int(cur.lastrowid)
    finally:
        conn.close()


# ── _recent_errors ────────────────────────────────────────────────────────────


class TestRecentErrors:
    def test_no_error_log(self, tmp_path, monkeypatch):
        from estormi_server.services import pipeline_status as pipeline

        monkeypatch.setattr(pipeline, "DAG_ERR_LOG_CANDIDATES", [tmp_path / "nope.log"])
        assert _recent_errors() == []

    def test_reads_errors(self, tmp_path, monkeypatch):
        from estormi_server.services import pipeline_status as pipeline

        err_log = tmp_path / "err.log"
        err_log.write_text("error 1\nerror 2\n\nerror 3")
        monkeypatch.setattr(pipeline, "DAG_ERR_LOG_CANDIDATES", [err_log])
        errors = _recent_errors()
        assert len(errors) == 3
        assert "error 1" in errors


# ── get_pipeline_data ─────────────────────────────────────────────────────────


class TestGetPipelineData:
    def test_no_runs(self, dag_db):
        data = get_pipeline_data()
        assert data["is_running"] is False
        assert data["overall_status"] == "unknown"
        assert data["run_count"] == 0

    def test_completed_run(self, dag_db):
        run_id = _insert_run(
            dag_db,
            started_at="2024-06-15T02:00:00+00:00",
            ended_at="2024-06-15T02:00:15+00:00",
            status="ok",
            duration_ms=15_000,
        )
        _insert_stage(
            dag_db,
            run_id,
            "notes",
            started_at="2024-06-15T02:00:00+00:00",
            ended_at="2024-06-15T02:00:10+00:00",
            status="ok",
            duration_ms=10_000,
        )
        data = get_pipeline_data()
        assert data["is_running"] is False
        assert data["overall_status"] == "ok"
        assert data["last_run_duration_s"] == 15
        assert data["run_count"] == 1

    def test_failed_run(self, dag_db):
        run_id = _insert_run(
            dag_db,
            started_at="2024-06-15T02:00:00+00:00",
            ended_at="2024-06-15T02:00:10+00:00",
            status="failed",
            duration_ms=10_000,
        )
        _insert_stage(
            dag_db,
            run_id,
            "notes",
            started_at="2024-06-15T02:00:00+00:00",
            ended_at="2024-06-15T02:00:05+00:00",
            status="failed",
            duration_ms=5_000,
        )
        data = get_pipeline_data()
        assert data["overall_status"] == "fail"
        assert "notes" in data["last_run_failed_stages"]
