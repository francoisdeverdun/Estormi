"""Integration tests for FastAPI routes — /health, /ingest_chunk, /search_memory, /mcp."""

from __future__ import annotations

import json

import pytest

pytestmark = pytest.mark.integration

# ── Health ──────────────────────────────────────────────────────────────────


class TestHealthEndpoint:
    async def test_health_returns_ok(self, client):
        resp = await client.get("/health")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] in ("ok", "degraded")
        assert "sqlite" in body


# ── Ingest Chunk REST ───────────────────────────────────────────────────────


class TestIngestChunkRest:
    async def test_ingest_valid_chunk(self, client, mock_qdrant):
        body = {
            "text": "Alice met Bob at the café.",
            "source": "test",
            "content_hash": "rest-hash-0",
            "title": "Meeting notes",
            "date": "2024-06-15T10:00:00Z",
        }
        resp = await client.post("/ingest_chunk", json=body)
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert "id" in data

    async def test_ingest_missing_required_fields(self, client):
        resp = await client.post("/ingest_chunk", json={"text": "hello"})
        assert resp.status_code == 422  # Pydantic validation

    async def test_ingest_empty_text(self, client):
        body = {"text": "   ", "source": "test", "content_hash": "e-0"}
        resp = await client.post("/ingest_chunk", json=body)
        assert resp.status_code == 200
        assert resp.json()["status"] == "skipped"

    async def test_ingest_duplicate_skipped(self, client, mock_qdrant):
        body = {
            "text": "duplicate test",
            "source": "test",
            "content_hash": "dup-rest-0",
        }
        r1 = await client.post("/ingest_chunk", json=body)
        assert r1.json()["status"] == "ok"

        r2 = await client.post("/ingest_chunk", json=body)
        assert r2.json()["status"] == "skipped"
        assert r2.json()["reason"] == "duplicate"


# ── Search Memory REST ──────────────────────────────────────────────────────


class TestSearchMemoryRest:
    async def test_search_returns_list(self, client, mock_qdrant):
        body = {"query": "meeting notes"}
        resp = await client.post("/search_memory", json=body)
        assert resp.status_code == 200
        assert isinstance(resp.json(), list)

    async def test_search_missing_query(self, client):
        resp = await client.post("/search_memory", json={})
        assert resp.status_code == 422

    async def test_search_with_filters(self, client, mock_qdrant):
        body = {
            "query": "test",
            "source": "imessage",
            "after": "2024-01-01",
            "before": "2024-12-31",
            "limit": 5,
        }
        resp = await client.post("/search_memory", json=body)
        assert resp.status_code == 200


# ── MCP JSON-RPC ────────────────────────────────────────────────────────────


class TestMcpRpc:
    async def test_initialize(self, client):
        rpc = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {},
        }
        resp = await client.post("/mcp", json=rpc)
        assert resp.status_code == 200
        data = resp.json()
        assert data["jsonrpc"] == "2.0"
        assert data["id"] == 1
        assert "result" in data
        assert data["result"]["serverInfo"]["name"] == "estormi"

    async def test_tools_list(self, client):
        rpc = {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/list",
            "params": {},
        }
        resp = await client.post("/mcp", json=rpc)
        data = resp.json()
        assert "result" in data
        tools_list = data["result"]["tools"]
        tool_names = {t["name"] for t in tools_list}
        assert "search_memory" in tool_names
        assert "ingest_chunk" in tool_names
        assert "delete_by_source" in tool_names

    async def test_tools_call_search(self, client, mock_qdrant):
        rpc = {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {
                "name": "search_memory",
                "arguments": {"query": "hello world"},
            },
        }
        resp = await client.post("/mcp", json=rpc)
        data = resp.json()
        assert "result" in data
        assert data["result"]["isError"] is False

    async def test_tools_call_ingest(self, client, mock_qdrant):
        rpc = {
            "jsonrpc": "2.0",
            "id": 4,
            "method": "tools/call",
            "params": {
                "name": "ingest_chunk",
                "arguments": {
                    "text": "MCP ingest test",
                    "source": "test",
                    "content_hash": "mcp-hash-0",
                },
            },
        }
        resp = await client.post("/mcp", json=rpc)
        data = resp.json()
        assert "result" in data
        content = json.loads(data["result"]["content"][0]["text"])
        assert content["status"] == "ok"

    async def test_unknown_tool(self, client):
        rpc = {
            "jsonrpc": "2.0",
            "id": 5,
            "method": "tools/call",
            "params": {
                "name": "nonexistent_tool",
                "arguments": {},
            },
        }
        resp = await client.post("/mcp", json=rpc)
        # An unknown tool is reported as a JSON-RPC error object (HTTP 200),
        # not a bare HTTP 404 — MCP clients speak JSON-RPC and cannot parse a
        # raw HTTP error.
        assert resp.status_code == 200
        body = resp.json()
        assert body["id"] == 5
        assert "error" in body
        assert body["error"]["code"] == -32602
        assert "nonexistent_tool" in body["error"]["message"]

    async def test_tools_list_includes_new_tools(self, client):
        rpc = {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}
        resp = await client.post("/mcp", json=rpc)
        tool_names = {t["name"] for t in resp.json()["result"]["tools"]}
        assert "delete_chunk" in tool_names
        assert "get_chunk" in tool_names

    async def test_unknown_method(self, client):
        rpc = {
            "jsonrpc": "2.0",
            "id": 6,
            "method": "unknown/method",
            "params": {},
        }
        resp = await client.post("/mcp", json=rpc)
        data = resp.json()
        assert "error" in data
        assert data["error"]["code"] == -32601


# ── MCP/REST contract regressions (sweep 2 U16/U17/U18) ───────────────────────


class TestFetchAroundWindowClamp:
    """U17 — fetch_around clamps window_days to the schema maximum (90)."""

    async def test_window_days_clamped_to_90(self, wired_tools_db, mock_qdrant):
        from estormi_server.storage.search_api import fetch_around
        from estormi_server.storage.writers import ingest_chunk

        # Center 2026-05-20. The contract caps window_days at 90 (raised from 30
        # to cover the briefing's ~2-month correlation forward horizon). A chunk
        # +80d is inside the cap and must be admitted; one +120d is beyond it and
        # must be excluded even when the caller asks for an absurd ±100000d.
        await ingest_chunk(
            text="near",
            source="calendar",
            content_hash="near-0",
            source_id="near-0",
            date="2026-05-20T12:00:00Z",
        )
        await ingest_chunk(
            text="eighty days away",
            source="mail",
            content_hash="in-0",
            source_id="in-0",
            date="2026-08-08T12:00:00Z",  # +80 days — inside the 90d cap
        )
        await ingest_chunk(
            text="hundred-twenty days away",
            source="notes",
            content_hash="far-0",
            source_id="far-0",
            date="2026-09-17T12:00:00Z",  # +120 days — beyond the 90d cap
        )
        out = await fetch_around("2026-05-20", window_days=100000)
        sources = {c["source"] for c in out}
        assert "calendar" in sources
        assert "mail" in sources  # +80d now inside the clamped ±90d window
        assert "notes" not in sources  # +120d outside the clamped ±90d window


class TestUnparseableDate:
    """U18 — fetch_around raises HTTP 400 for an unparseable required 'date'."""

    async def test_direct_call_raises_400(self, wired_tools_db, mock_qdrant):
        from fastapi import HTTPException

        from estormi_server.storage.search_api import fetch_around

        with pytest.raises(HTTPException) as exc:
            await fetch_around("2026-13-99")
        assert exc.value.status_code == 400

    async def test_rest_returns_400(self, client, mock_qdrant):
        resp = await client.post("/fetch_around", json={"date": "2026-13-99"})
        assert resp.status_code == 400

    async def test_mcp_bad_date_is_invalid_params(self, client, mock_qdrant):
        rpc = {
            "jsonrpc": "2.0",
            "id": 7,
            "method": "tools/call",
            "params": {"name": "fetch_around", "arguments": {"date": "yesterday"}},
        }
        resp = await client.post("/mcp", json=rpc)
        assert resp.status_code == 200
        body = resp.json()
        # Must be a JSON-RPC error, not a successful empty result.
        assert "error" in body, body
        assert body["error"]["code"] == -32602


class TestMissingArgumentIsInvalidParams:
    """U16 — MCP tools/call maps client-input errors to JSON-RPC -32602."""

    async def test_search_memory_missing_query_is_invalid_params(self, client, mock_qdrant):
        rpc = {
            "jsonrpc": "2.0",
            "id": 8,
            "method": "tools/call",
            "params": {"name": "search_memory", "arguments": {}},
        }
        resp = await client.post("/mcp", json=rpc)
        assert resp.status_code == 200
        body = resp.json()
        assert "error" in body, body
        assert body["error"]["code"] == -32602  # not -32000

    async def test_fetch_around_missing_date_is_invalid_params(self, client, mock_qdrant):
        rpc = {
            "jsonrpc": "2.0",
            "id": 9,
            "method": "tools/call",
            "params": {"name": "fetch_around", "arguments": {}},
        }
        resp = await client.post("/mcp", json=rpc)
        assert resp.status_code == 200
        body = resp.json()
        assert "error" in body, body
        assert body["error"]["code"] == -32602
