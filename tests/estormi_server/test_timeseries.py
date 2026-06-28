"""HTTP tests for ``GET /api/timeseries`` — the macOS IngestionPulse data.

Two views over the same window: ``mode=ingestion`` plots per-day chunks
*added* (a daily delta), ``mode=memory`` (default) plots the cumulative
*store* per source so the stack climbs to the all-time total — the "total
over time" idiom the iOS Memoria card uses. These tests pin both the shape
and the cumulative arithmetic so the two modes can't silently swap.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

pytestmark = pytest.mark.integration


async def _seed(db) -> None:
    """One source seeded before the window (baseline) + same-day adds."""
    now = datetime.now(timezone.utc)
    today = now.strftime("%Y-%m-%d %H:%M:%S")
    old = (now - timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S")
    rows = [
        ("c1", "h1", "notes", "personal", old),  # baseline — outside window
        ("c2", "h2", "notes", "personal", today),
        ("c3", "h3", "notes", "personal", today),
        ("c4", "h4", "mail", "personal", today),
    ]
    for cid, h, source, corpus, ts in rows:
        await db.execute(
            "INSERT INTO chunks (id, content_hash, source, corpus, ingested_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (cid, h, source, corpus, ts),
        )
    await db.commit()


async def test_memory_mode_is_cumulative(client, db):
    await _seed(db)
    r = await client.get("/api/timeseries?days=14&mode=memory")
    assert r.status_code == 200
    body = r.json()

    assert len(body["days"]) == 14
    # Sources ordered by all-time count, busiest first.
    assert body["sources"] == ["notes", "mail"]
    # Cumulative store — the last day lands on the all-time totals (4 chunks),
    # including the baseline seeded before the window.
    last = body["series"][-1]
    assert last["total"] == 4
    assert last["by_source"] == {"notes": 3, "mail": 1}
    # The store never decreases across the window.
    totals = [p["total"] for p in body["series"]]
    assert totals == sorted(totals)


async def test_memory_is_the_default_mode(client, db):
    await _seed(db)
    default = (await client.get("/api/timeseries?days=14")).json()
    explicit = (await client.get("/api/timeseries?days=14&mode=memory")).json()
    assert default == explicit


async def test_ingestion_mode_counts_only_window_adds(client, db):
    await _seed(db)
    body = (await client.get("/api/timeseries?days=14&mode=ingestion")).json()
    # Only the three same-day chunks fall inside the window; the 30-day-old
    # baseline is excluded from the ingestion deltas.
    assert sum(p["total"] for p in body["series"]) == 3
