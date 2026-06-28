"""Tests for POST /ingest_batch — bulk ingestion endpoint."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.integration


async def test_batch_ingests_multiple_chunks(client):
    chunks = [
        {
            "text": f"batch test chunk {i}",
            "source": "test-batch",
            "source_id": f"batch-{i}",
            "title": f"Batch chunk {i}",
            "date": "2026-06-24",
            "content_hash": f"batchhash-{i}",
        }
        for i in range(3)
    ]
    resp = await client.post("/ingest_batch", json={"chunks": chunks})
    assert resp.status_code == 200
    data = resp.json()
    assert data["count"] == 3
    assert len(data["results"]) == 3


async def test_batch_empty_list(client):
    resp = await client.post("/ingest_batch", json={"chunks": []})
    assert resp.status_code == 200
    assert resp.json()["count"] == 0


async def test_batch_rejects_over_100(client):
    chunks = [
        {
            "text": f"chunk {i}",
            "source": "test",
            "source_id": f"over-{i}",
            "title": "",
            "date": "",
            "content_hash": f"over-{i}",
        }
        for i in range(101)
    ]
    resp = await client.post("/ingest_batch", json={"chunks": chunks})
    assert resp.status_code == 422


async def test_batch_csrf_required(client):
    resp = await client.post(
        "/ingest_batch",
        json={"chunks": []},
        headers={"X-Estormi-Origin": "", "Authorization": ""},
    )
    assert resp.status_code == 403
