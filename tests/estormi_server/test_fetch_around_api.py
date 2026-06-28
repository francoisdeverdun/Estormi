"""API-layer coverage for the fetch_around retrieval surface + corpus filter.

Exercises the REST shim (``POST /fetch_around``), the MCP tool dispatch, the
tools catalog, and the ``corpus`` passthrough on ``/search_memory`` — the
surfaces added when correlation moved from a stored engine to time-window
retrieval.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.integration


async def _ingest(client, *, source, content_hash, date, text="hello world"):
    resp = await client.post(
        "/ingest_chunk",
        json={
            "text": text,
            "source": source,
            "content_hash": content_hash,
            "source_id": content_hash,
            "date": date,
        },
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


class TestFetchAroundRest:
    async def test_window_filters_by_date(self, client, mock_qdrant):
        await _ingest(client, source="calendar", content_hash="c-near", date="2026-05-20T20:00:00Z")
        await _ingest(client, source="mail", content_hash="c-far", date="2026-01-01T09:00:00Z")
        resp = await client.post("/fetch_around", json={"date": "2026-05-20", "window_days": 1})
        assert resp.status_code == 200
        out = resp.json()
        sources = {c["source"] for c in out}
        assert "calendar" in sources
        assert "mail" not in sources  # January outlier is outside ±1d

    async def test_corpus_filter(self, client, mock_qdrant):
        await _ingest(client, source="news", content_hash="w-1", date="2026-05-20T07:00:00Z")
        await _ingest(client, source="mail", content_hash="p-1", date="2026-05-20T07:00:00Z")
        world = await client.post(
            "/fetch_around", json={"date": "2026-05-20", "window_days": 1, "corpus": "world"}
        )
        personal = await client.post(
            "/fetch_around", json={"date": "2026-05-20", "window_days": 1, "corpus": "personal"}
        )
        assert {c["source"] for c in world.json()} == {"news"}
        assert {c["source"] for c in personal.json()} == {"mail"}

    async def test_source_filter(self, client, mock_qdrant):
        await _ingest(client, source="calendar", content_hash="s-cal", date="2026-05-20T08:00:00Z")
        await _ingest(client, source="reminders", content_hash="s-rem", date="2026-05-20T08:00:00Z")
        resp = await client.post(
            "/fetch_around",
            json={"date": "2026-05-20", "window_days": 2, "sources": ["calendar"]},
        )
        assert [c["source"] for c in resp.json()] == ["calendar"]

    async def test_unparseable_date_rejected(self, client, mock_qdrant):
        # An unparseable required 'date' surfaces as HTTP 400 through the REST
        # shim, not a silent 200/[] (sweep2 bug U18).
        resp = await client.post("/fetch_around", json={"date": "not-a-date"})
        assert resp.status_code == 400

    async def test_missing_date_rejected(self, client):
        resp = await client.post("/fetch_around", json={"window_days": 1})
        assert resp.status_code == 422

    async def test_extreme_year_date_rejected(self, client, mock_qdrant):
        # A parseable but extreme-year date overflows the window arithmetic
        # (date ± timedelta). That's a malformed client date → 400, not a 500.
        resp = await client.post("/fetch_around", json={"date": "9999-12-31", "window_days": 2})
        assert resp.status_code == 400


class TestFetchAroundMcp:
    async def test_in_tools_catalog(self, client):
        rpc = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
        resp = await client.post("/mcp", json=rpc)
        names = [t["name"] for t in resp.json()["result"]["tools"]]
        assert "fetch_around" in names
        # The deleted engines' tools are gone.
        assert "list_entities" not in names
        assert "annotate_entity" not in names

    async def test_tools_call_fetch_around(self, client, mock_qdrant):
        await _ingest(client, source="calendar", content_hash="m-1", date="2026-05-20T20:00:00Z")
        rpc = {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {
                "name": "fetch_around",
                "arguments": {"date": "2026-05-20", "window_days": 1},
            },
        }
        resp = await client.post("/mcp", json=rpc)
        assert resp.status_code == 200
        assert "result" in resp.json()


class TestSearchCorpus:
    async def test_search_memory_accepts_corpus(self, client, mock_qdrant):
        resp = await client.post("/search_memory", json={"query": "anything", "corpus": "personal"})
        assert resp.status_code == 200
        assert isinstance(resp.json(), list)
