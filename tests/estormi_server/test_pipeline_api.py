"""HTTP-level tests for :mod:`estormi_server.api.pipeline`.

The pipeline router was covered only by its data-model helpers; the route
handlers themselves — slug validation, the 409 duplicate-launch path, the
stage-log **path allow-list** (a path-traversal guard), and the timeseries
aggregation — were not. These exercise them over the ASGI ``client`` fixture
with ``jobs``/filesystem boundaries mocked.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

pytestmark = pytest.mark.integration


# ── /api/pipeline/run ───────────────────────────────────────────────────────
class TestPipelineRun:
    async def test_run_rejects_invalid_stage_slug(self, client):
        r = await client.post("/api/pipeline/run", params={"stage": "../etc/passwd"})
        assert r.status_code == 400
        assert "invalid stage" in r.json()["error"]

    async def test_run_enqueues_and_returns_status(self, client):
        with patch(
            "estormi_server.api.pipeline.jobs.enqueue",
            new=AsyncMock(return_value="queued"),
        ) as enq:
            r = await client.post("/api/pipeline/run", params={"stage": "notes"})
        assert r.status_code == 200
        assert r.json() == {"status": "queued", "stage": "notes"}
        enq.assert_awaited_once()

    async def test_run_duplicate_launch_is_409(self, client):
        with patch(
            "estormi_server.api.pipeline.jobs.enqueue",
            new=AsyncMock(return_value="already_running"),
        ):
            r = await client.post("/api/pipeline/run")
        assert r.status_code == 409
        assert r.json()["status"] == "already_running"


# ── /api/pipeline/stop ──────────────────────────────────────────────────────
class TestPipelineStop:
    async def test_stop_not_running_returns_409(self, client):
        with (
            patch("estormi_server.api.pipeline.jobs.remove_from_queue", new=AsyncMock()),
            patch("estormi_server.api.pipeline.jobs._dag_pgid", None),
            patch("estormi_server.api.pipeline.jobs._dag_proc", None),
            patch(
                "estormi_server.api.pipeline.jobs._locked_pgid",
                new=AsyncMock(return_value=None),
            ),
        ):
            r = await client.post("/api/pipeline/stop")
        assert r.status_code == 409
        assert r.json()["status"] == "not_running"

    async def test_stop_kills_running_pgid(self, client):
        with (
            patch("estormi_server.api.pipeline.jobs.remove_from_queue", new=AsyncMock()),
            patch("estormi_server.api.pipeline.jobs._dag_pgid", 4242),
            patch(
                "estormi_server.api.pipeline.jobs._kill_dag_processes", new=AsyncMock()
            ) as kill_dag,
        ):
            r = await client.post("/api/pipeline/stop")
        assert r.status_code == 200
        assert r.json()["status"] == "stopped"
        kill_dag.assert_awaited_once()


# ── /api/pipeline/stage-log — path allow-list (security) ────────────────────
class TestStageLogAllowList:
    async def test_requires_a_selector(self, client):
        r = await client.get("/api/pipeline/stage-log")
        assert r.status_code == 400

    async def test_rejects_invalid_source_slug(self, client):
        r = await client.get("/api/pipeline/stage-log", params={"source": "../../etc"})
        assert r.status_code == 400

    async def test_unknown_engine_rejected(self, client):
        r = await client.get("/api/pipeline/stage-log", params={"engine": "nope"})
        assert r.status_code == 400

    async def test_path_outside_allowlist_is_403(self, client):
        # A path traversal / arbitrary read attempt must be refused, not served.
        r = await client.get("/api/pipeline/stage-log", params={"path": "/etc/passwd"})
        assert r.status_code == 403
        assert r.json()["error"] == "path not allowed"

    async def test_tmp_path_without_estormi_prefix_is_403(self, client):
        r = await client.get("/api/pipeline/stage-log", params={"path": "/tmp/secret.log"})
        assert r.status_code == 403

    async def test_allowlisted_missing_file_is_404(self, client):
        r = await client.get(
            "/api/pipeline/stage-log",
            params={"path": "/tmp/estormi-does-not-exist.log"},
        )
        assert r.status_code == 404

    async def test_allowlisted_tmp_file_is_served(self, client, tmp_path):
        # /tmp/estormi-*.log is on the allow-list; create one and read it back.
        log = Path("/tmp") / "estormi-test-stagelog.log"
        log.write_text("line one\nline two\n")
        try:
            r = await client.get("/api/pipeline/stage-log", params={"path": str(log), "lines": 10})
            assert r.status_code == 200
            assert "line two" in r.json()["content"]
        finally:
            log.unlink(missing_ok=True)

    async def test_source_log_aggregation(self, client):
        with patch(
            "estormi_server.api.pipeline._aggregate_source_run_logs",
            return_value="aggregated tail",
        ):
            r = await client.get("/api/pipeline/stage-log", params={"source": "notes"})
        assert r.status_code == 200
        assert r.json() == {"path": "source:notes", "content": "aggregated tail"}


# ── /api/timeseries ─────────────────────────────────────────────────────────
class TestTimeseries:
    async def _seed(self, db):
        await db.executemany(
            "INSERT INTO chunks (id, content_hash, source, ingested_at) VALUES (?, ?, ?, ?)",
            [
                ("a", "ha", "notes", "2026-01-01 10:00:00"),
                ("b", "hb", "notes", "2026-01-01 11:00:00"),
                ("c", "hc", "mail", "2026-01-02 09:00:00"),
            ],
        )
        await db.commit()

    async def test_memory_mode_is_cumulative(self, client, db):
        await self._seed(db)
        r = await client.get("/api/timeseries", params={"days": 400, "mode": "memory"})
        assert r.status_code == 200
        body = r.json()
        # Cumulative store ends on the all-time total per source.
        last = body["series"][-1]["by_source"]
        assert last.get("notes") == 2
        assert last.get("mail") == 1

    async def test_ingestion_mode_is_daily_delta(self, client, db):
        await self._seed(db)
        r = await client.get("/api/timeseries", params={"days": 400, "mode": "ingestion"})
        assert r.status_code == 200
        body = r.json()
        totals = sum(s["total"] for s in body["series"])
        assert totals == 3


# ── /api/sources/{name}/watermark/reset ─────────────────────────────────────
class TestWatermarkReset:
    async def test_rejects_invalid_slug(self, client):
        r = await client.put("/api/sources/..%2F..%2Fetc/watermark/reset")
        assert r.status_code in (400, 404)

    async def test_resets_watermark(self, client, db):
        await db.execute(
            "INSERT INTO ingestion_watermarks (source, last_fetched_at) "
            "VALUES ('notes', '2026-01-01')"
        )
        await db.commit()
        r = await client.put("/api/sources/notes/watermark/reset")
        assert r.status_code == 200
        assert r.json()["status"] == "ok"
        rows = await db.execute_fetchall(
            "SELECT * FROM ingestion_watermarks WHERE source = 'notes'"
        )
        assert rows == []

    async def test_whatsapp_targets_log_watermark(self, client, db):
        await db.execute(
            "INSERT INTO ingestion_watermarks (source, last_fetched_at) "
            "VALUES ('whatsapp_log', '2026-01-01')"
        )
        await db.commit()
        r = await client.put("/api/sources/whatsapp/watermark/reset")
        assert r.status_code == 200
        rows = await db.execute_fetchall(
            "SELECT * FROM ingestion_watermarks WHERE source = 'whatsapp_log'"
        )
        assert rows == []
