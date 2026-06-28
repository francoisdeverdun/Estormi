"""End-to-end API scenario across ingest → fetch → delete.

Exercises the live storage path end to end: ingest over the REST
``/ingest_chunk`` endpoint (asserting the Qdrant upsert actually fired with the
right payload), then fetch and delete the chunk through the MCP ``tools/call``
surface (``get_chunk`` / ``delete_chunk``) — the routes a real client uses now
that the unused ``/api/chunks`` REST CRUD has been removed. A genuine
vector-search e2e would need an in-memory Qdrant instance and lives behind a
future ticket.
"""

from __future__ import annotations

import json

import pytest

pytestmark = pytest.mark.e2e


def _tool_call(client, name: str, arguments: dict):
    """POST an MCP ``tools/call`` request and return the (awaitable) response."""
    return client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        },
    )


class TestMemoryLifecycle:
    async def test_ingest_fetch_and_delete_chunk(self, client, mock_qdrant):
        ingest = await client.post(
            "/ingest_chunk",
            json={
                "text": "Lifecycle probe about a durable project decision.",
                "source": "manual",
                "source_id": "lifecycle-001",
                "content_hash": "lifecycle-hash-001",
                "title": "Lifecycle Probe",
                "date": "2026-05-06T10:00:00+00:00",
            },
        )
        assert ingest.status_code == 200
        chunk_id = ingest.json()["id"]

        # Qdrant upsert was actually called (storage path exercised).
        assert mock_qdrant.upsert.await_count == 1
        stored_point = mock_qdrant.upsert.call_args.kwargs["points"][0]
        assert stored_point.payload["source"] == "manual"
        assert stored_point.payload["source_id"] == "lifecycle-001"

        # Fetch through the MCP ``get_chunk`` tool, which joins the SQL row with
        # the Qdrant payload text — wire ``retrieve`` to echo the upsert so the
        # body comes back (the SQL row alone holds no text).
        mock_qdrant.retrieve.return_value = [stored_point]
        fetched = await _tool_call(client, "get_chunk", {"id": chunk_id})
        assert fetched.status_code == 200
        result = fetched.json()["result"]
        assert result["isError"] is False
        chunk = json.loads(result["content"][0]["text"])
        assert chunk["id"] == chunk_id
        assert chunk["source"] == "manual"
        assert "durable project decision" in chunk["text"]

        # Delete through the MCP ``delete_chunk`` tool.
        deleted = await _tool_call(client, "delete_chunk", {"id": chunk_id})
        assert deleted.status_code == 200
        assert deleted.json()["result"]["isError"] is False

        # Now gone: ``get_chunk`` surfaces a JSON-RPC not-found error.
        missing = await _tool_call(client, "get_chunk", {"id": chunk_id})
        assert missing.status_code == 200
        err = missing.json()["error"]
        assert err["code"] == -32602
        assert "not found" in err["message"].lower()
