"""Tests for MCP tool argument validation against inputSchema."""

from __future__ import annotations

import pytest


def _rpc(name: str, arguments: dict) -> dict:
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": name, "arguments": arguments},
    }


@pytest.mark.unit
async def test_missing_required_argument(client):
    """search_memory requires 'query'; omitting it should yield -32602."""
    resp = await client.post("/mcp", json=_rpc("search_memory", {}))
    body = resp.json()
    assert body["error"]["code"] == -32602
    assert (
        "query" in body["error"]["message"].lower()
        or "required" in body["error"]["message"].lower()
    )


@pytest.mark.unit
async def test_wrong_type_argument(client):
    """search_memory.limit must be integer; a string should yield -32602."""
    resp = await client.post(
        "/mcp", json=_rpc("search_memory", {"query": "hi", "limit": "not_int"})
    )
    body = resp.json()
    assert body["error"]["code"] == -32602


@pytest.mark.unit
async def test_valid_arguments_pass_through(client, monkeypatch):
    """Valid arguments should reach _dispatch_tool (we stub it to avoid real DB)."""
    called: dict = {}

    async def fake_dispatch(name, arguments):
        called["name"] = name
        called["args"] = arguments
        return {"results": []}

    monkeypatch.setattr("estormi_server.api.mcp_rpc._dispatch_tool", fake_dispatch)
    resp = await client.post("/mcp", json=_rpc("search_memory", {"query": "test"}))
    body = resp.json()
    assert "error" not in body
    assert called["name"] == "search_memory"
