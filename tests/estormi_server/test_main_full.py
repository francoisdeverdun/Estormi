"""Tests for main.py — MCP RPC, settings API, helper functions."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from estormi_server.api.mcp_rpc import (
    _dispatch_tool,
    _rpc_error,
    _rpc_result,
)
from estormi_server.server.jobs import _run_dag

pytestmark = pytest.mark.integration

# ── _rpc_result / _rpc_error (pure) ──────────────────────────────────────────


class TestRpcHelpers:
    def test_rpc_result_basic(self):
        r = _rpc_result(1, {"tools": []})
        assert r["jsonrpc"] == "2.0"
        assert r["id"] == 1
        assert r["result"]["tools"] == []

    def test_rpc_result_none_id(self):
        r = _rpc_result(None, "ok")
        assert r["id"] is None

    def test_rpc_error_basic(self):
        r = _rpc_error(42, -32601, "Method not found")
        assert r["jsonrpc"] == "2.0"
        assert r["id"] == 42
        assert r["error"]["code"] == -32601
        assert r["error"]["message"] == "Method not found"


# ── _dispatch_tool ────────────────────────────────────────────────────────────


class TestDispatchTool:
    async def test_search_memory(self):
        mock_search = AsyncMock(return_value={"results": []})
        with patch("estormi_server.api.mcp_rpc._search_memory", mock_search):
            result = await _dispatch_tool("search_memory", {"query": "test"})
        assert result == {"results": []}
        mock_search.assert_called_once()

    async def test_ingest_chunk(self):
        mock_ingest = AsyncMock(return_value={"status": "ok"})
        with patch("estormi_server.api.mcp_rpc._ingest_chunk", mock_ingest):
            result = await _dispatch_tool(
                "ingest_chunk",
                {"text": "t", "source": "s", "content_hash": "h"},
            )
        assert result["status"] == "ok"

    async def test_delete_by_source(self):
        mock_del = AsyncMock(return_value={"deleted": 5})
        with patch("estormi_server.api.mcp_rpc.delete_by_source", mock_del):
            result = await _dispatch_tool("delete_by_source", {"source": "test"})
        assert result["deleted"] == 5

    async def test_unknown_tool_raises(self):
        from fastapi import HTTPException

        with pytest.raises(HTTPException, match="Unknown tool"):
            await _dispatch_tool("nonexistent_tool", {})


# ── MCP RPC endpoint (via client fixture) ────────────────────────────────────
#
# The plain-smoke tests (test_initialize / test_tools_list / test_unknown_method
# and the remote-MCP bearer cases) live in `tests/estormi_server/test_api.py::TestMcpRpc` and
# `tests/estormi_server/test_security_boundary.py`. This class keeps only the
# higher-value scenarios — the real `tools/call` round-trip and the assertion
# that `tools/list` advertises the full group_type enum.


class TestMcpRpcEndpoint:
    async def test_tools_list_advertises_group_type_enum(self, client):
        payload = {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/list",
            "params": {},
        }
        resp = await client.post("/mcp", json=payload)
        assert resp.status_code == 200
        data = resp.json()
        tools = data["result"]["tools"]
        search_tool = next(t for t in tools if t["name"] == "search_memory")
        group_type = search_tool["inputSchema"]["properties"]["group_type"]
        assert "couple" in group_type["enum"]
        assert "organisation" in group_type["enum"]
        assert "unknown" in group_type["enum"]
        assert "couple concerns the household" in group_type["description"]

    async def test_tools_call(self, client):
        """Real round-trip through ``tools/call`` → ``search_memory``.

        Previously this mocked ``_search_memory`` so it tested wiring only.
        Now we actually ingest a chunk, then call the MCP RPC endpoint, then
        assert the JSON-RPC envelope AND that the underlying search executed
        without error and returned a structured response.
        """
        # Seed the in-memory DB so search has something to look at. We rely
        # on the existing ``mock_embedder`` + ``mock_qdrant`` fixtures
        # transitively pulled in by ``client``.
        ingest = await client.post(
            "/ingest_chunk",
            json={
                "text": "tools/call integration probe",
                "source": "tools-call-test",
                "content_hash": "tools-call-001",
            },
        )
        assert ingest.status_code == 200

        payload = {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {
                "name": "search_memory",
                "arguments": {"query": "tools/call integration"},
            },
        }
        resp = await client.post("/mcp", json=payload)
        assert resp.status_code == 200
        data = resp.json()

        # JSON-RPC envelope contract.
        assert data["jsonrpc"] == "2.0"
        assert data["id"] == 3
        # MCP tool-call contract: ``isError`` flag is present and false on
        # success, and the structured content carries the search result.
        assert data["result"]["isError"] is False
        # New behavioural assertion: the response carries structured content
        # produced by the real ``search_memory`` path.
        assert "content" in data["result"]
        assert isinstance(data["result"]["content"], list)


# ── REST API endpoints ────────────────────────────────────────────────────────


class TestRestApi:
    async def test_health(self, client):
        resp = await client.get("/health")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"

    async def test_get_settings(self, client):
        resp = await client.get("/api/settings")
        assert resp.status_code == 200
        data = resp.json()
        assert "setup_completed" in data

    async def test_put_settings(self, client):
        resp = await client.put("/api/settings", json={"test_key": "test_value"})
        assert resp.status_code == 200
        # Verify persisted
        resp2 = await client.get("/api/settings")
        assert resp2.json()["test_key"] == "test_value"

    async def test_ingest_and_search(self, client, db):
        """End-to-end ingest → SQLite → search through real FastAPI handlers.

        Previously the test only asserted ``200`` from both endpoints. That
        let a regression where the chunk silently failed to persist slip
        through. We now also verify the chunk landed in SQLite under the
        expected ``content_hash`` and that the search endpoint returns a
        JSON-shaped response (the dense/sparse vectors are mocked at the
        fixture level, so we cannot assert on retrieval ranking — but we
        can and do assert on the storage path).
        """
        body = {
            "text": "The quick brown fox jumps over the lazy dog",
            "source": "test",
            "title": "test doc",
            "content_hash": "test-hash-001",
        }
        resp = await client.post("/ingest_chunk", json=body)
        assert resp.status_code == 200

        # New behavioural assertion: the chunk's metadata is persisted in
        # SQLite (the full text lives in Qdrant, not the relational store).
        cursor = await db.execute(
            "SELECT source, title FROM chunks WHERE content_hash = ?",
            ("test-hash-001",),
        )
        row = await cursor.fetchone()
        await cursor.close()
        assert row is not None, "ingest_chunk did not persist metadata to SQLite"
        assert row["source"] == "test"
        assert row["title"] == "test doc"

        # Search endpoint flows through real query construction.
        resp = await client.post(
            "/search_memory",
            json={"query": "brown fox", "limit": 5},
        )
        assert resp.status_code == 200
        # Real response shape contract — must be a JSON list (results array).
        payload = resp.json()
        assert isinstance(payload, list)

        # Duplicate ingest under the same content_hash must NOT create a
        # second row — the dedupe contract is enforced at the DB layer.
        resp2 = await client.post("/ingest_chunk", json=body)
        assert resp2.status_code == 200
        cursor = await db.execute(
            "SELECT COUNT(*) AS n FROM chunks WHERE content_hash = ?",
            ("test-hash-001",),
        )
        row = await cursor.fetchone()
        await cursor.close()
        assert row["n"] == 1, "duplicate content_hash must dedupe at the storage layer"


# ── put_settings scheduler integration ────────────────────────────────────────


class TestPutSettingsScheduler:
    """Cron-change rescheduling — driven through the real ``PUT /api/settings``
    endpoint and the real ``AsyncIOScheduler`` instance, with only the *outer*
    seam (``CronTrigger.from_crontab``-consumed value) observable via the
    scheduler's own ``get_job`` API. We do not stub the scheduler.
    """

    async def test_reschedules_dag_on_cron_change(self, client):
        """A cron-changing PUT must produce a registered ``daily_dag`` job.

        Previously this called ``put_settings`` directly with a MagicMock
        scheduler and only verified that ``reschedule_job`` was called with
        the right id. That tested wiring, not behaviour — a regression that
        forgot to actually apply the cron would still pass.

        We now hit the HTTP endpoint, then ask the real scheduler for the
        job and inspect its trigger.
        """
        from estormi_server.server import jobs as jobs_mod

        # Clean slate — make sure no prior test left a job registered.
        if jobs_mod._scheduler.get_job("daily_dag"):
            jobs_mod._scheduler.remove_job("daily_dag")

        try:
            resp = await client.put(
                "/api/settings",
                json={"schedule_cron": "0 3 * * *"},
            )
            assert resp.status_code == 200
            # Echoed payload contract.
            assert resp.json()["schedule_cron"] == "0 3 * * *"

            # Behavioural assertion: the real scheduler now owns a job with
            # the exact id and a CronTrigger whose hour matches the request.
            job = jobs_mod._scheduler.get_job("daily_dag")
            assert job is not None, "daily_dag must be registered after cron PUT"
            trigger_repr = repr(job.trigger)
            assert "hour='3'" in trigger_repr or "hour=3" in trigger_repr, (
                f"Expected hour=3 in trigger; got {trigger_repr!r}"
            )

            # Now change it again — the same job id must be reused (no dupes).
            resp2 = await client.put(
                "/api/settings",
                json={"schedule_cron": "0 5 * * *"},
            )
            assert resp2.status_code == 200
            job2 = jobs_mod._scheduler.get_job("daily_dag")
            assert job2 is not None
            trigger_repr2 = repr(job2.trigger)
            assert "hour='5'" in trigger_repr2 or "hour=5" in trigger_repr2

            # And the manual switch removes the job entirely.
            resp3 = await client.put(
                "/api/settings",
                json={"schedule_cron": "manual"},
            )
            assert resp3.status_code == 200
            assert jobs_mod._scheduler.get_job("daily_dag") is None
        finally:
            if jobs_mod._scheduler.get_job("daily_dag"):
                jobs_mod._scheduler.remove_job("daily_dag")


# ── put_settings re-probes a folder-rooted source's permission ────────────────


class TestPutSettingsReprobesRoot:
    """Writing `documents_root` via PUT /api/settings (the folder picker path)
    must re-probe the source's macOS permission, so the run gate stops reading
    the stale toggle-time status. Regression for documents being skipped with
    "permission undetermined" after a folder was picked."""

    async def test_root_write_reprobes_enabled_source(self, client):
        # Enable the source and set its root in one PUT; the re-probe fires for
        # the `_root` key and reads the just-committed enabled flag.
        with patch(
            "estormi_server.server.permissions.ensure_source_permission",
            return_value={"key": "FilesAndFolders", "status": "authorized", "settings_pane": None},
        ) as mock_probe:
            resp = await client.put(
                "/api/settings",
                json={"source_documents_enabled": "true", "documents_root": "/tmp/docs"},
            )
        assert resp.status_code == 200
        mock_probe.assert_called_once_with("documents", "/tmp/docs")

        # Ground truth persisted for the gate to read.
        import json

        from estormi_server.storage import tools

        async with tools._db.execute(
            "SELECT value FROM settings WHERE key = 'source_documents_permission'"
        ) as cur:
            row = await cur.fetchone()
        assert json.loads(row[0])["status"] == "authorized"

    async def test_non_root_write_does_not_probe(self, client):
        with patch("estormi_server.server.permissions.ensure_source_permission") as mock_probe:
            resp = await client.put("/api/settings", json={"some_unrelated_key": "v"})
        assert resp.status_code == 200
        mock_probe.assert_not_called()


# ── _run_dag log-file regression ──────────────────────────────────────────────


class TestRunDagLogsToFile:
    """Regression: _run_dag must write stdout directly to the DAG log file.

    Previously it used stdout=PIPE (never read), which could deadlock and
    left the log empty, causing the dashboard to show "No runs yet".
    """

    async def test_dag_stdout_written_to_log_file(self, tmp_path):
        log_file = tmp_path / "estormi-daily-dag.log"
        err_file = tmp_path / "estormi-daily-dag-error.log"

        scripts_dir = tmp_path / "scripts"
        scripts_dir.mkdir()
        script = scripts_dir / "daily_ingestion.sh"
        script.write_text(
            '#!/bin/bash\necho "[dag] starting daily ingestion DAG at 2026-05-07T02:00:00+0000"\n'
        )
        script.chmod(0o755)

        # The ``_run_dag`` body now reads every setting at once via the
        # ``_settings_snapshot`` helper (so the env-var translation is
        # consistent between the DAG path and the per-source ingest
        # endpoint). The patch target moved with it — stubbing
        # ``_get_setting`` on the old code path is now a no-op.
        all_enabled_snapshot = {
            f"source_{key}_enabled": "true"
            for key in (
                "notes",
                "mail",
                "reminders",
                "imessage",
                "whatsapp",
                "documents",
            )
        }
        with (
            patch("estormi_server.server.jobs._DAG_MAIN_LOG", log_file),
            patch("estormi_server.server.jobs._DAG_ERR_LOG", err_file),
            patch("estormi_server.server.jobs.ROOT", tmp_path),
            patch(
                "estormi_server.server.jobs._settings_snapshot",
                AsyncMock(return_value=all_enabled_snapshot),
            ),
            patch("estormi_server.server.jobs._dag_lock") as mock_lock,
        ):
            mock_lock.locked.return_value = False
            mock_lock.__aenter__ = AsyncMock(return_value=None)
            mock_lock.__aexit__ = AsyncMock(return_value=False)
            await _run_dag()

        content = log_file.read_text()
        assert "[dag] starting daily ingestion DAG at" in content, (
            "DAG log file must contain the start marker — stdout was likely sent to a pipe instead of the file"
        )
