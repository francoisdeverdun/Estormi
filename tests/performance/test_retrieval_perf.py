"""Performance benchmark: real memory-retrieval latency.

Exercises the production ``search_memory`` tool from ``estormi_server/storage/tools.py``
against a populated mock Qdrant backend (mirroring how ``tests/estormi_server/test_tools.py``
sets up its search tests). The benchmark covers the real RRF fusion, result
sanitisation, and recency scoring — a regression in any of those
post-processing steps would slow this down or break it.
"""

from __future__ import annotations

import time
import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from estormi_server.storage.search_api import search_memory

pytestmark = pytest.mark.performance

# Deliberately loose ceilings. The mocked backend removes all network and
# model latency, so a healthy run finishes these in single-digit to low-tens
# of milliseconds — orders of magnitude under the thresholds below. The point
# of the gate is NOT to measure absolute speed (a shared/loaded CI runner can
# stall an event loop for hundreds of ms through no fault of the code) but to
# catch a *post-processing* blow-up — an accidental O(n^2) in RRF fusion,
# sanitisation, or recency scoring — which would overshoot even these wide
# ceilings by a wide margin. Keep them generous so the suite never flakes
# spuriously under load.
MAX_SINGLE_SEARCH_MS = 5000
MAX_BATCH_SEARCH_MS = 30000


def _make_points(count: int) -> list[MagicMock]:
    """Build *count* synthetic Qdrant result points for the search to process."""
    points = []
    for i in range(count):
        point = MagicMock()
        point.id = str(uuid.uuid4())
        point.score = 0.9 - (i * 0.001)
        point.payload = {
            "text": f"Memory fragment {i}: Alice met Bob about a durable decision.",
            "source": "imessage",
            "source_id": f"msg-{i}",
            "title": f"Chat {i}",
            "date": "2026-05-06T10:00:00Z",
            "date_ts": "2026-05-06T10:00:00+00:00",
            "url": "",
            "group_type": None,
            "pending_reply": None,
        }
        points.append(point)
    return points


@pytest.fixture
def _wire_tools(wired_tools_db, mock_qdrant):
    """Yield ``mock_qdrant`` with ``tools._db`` wired up.

    Thin wrapper around the shared ``wired_tools_db`` fixture so existing
    tests below can keep their ``_wire_tools(mock_qdrant=...)`` contract.
    """
    yield mock_qdrant


async def test_search_memory_single_query_latency(_wire_tools):
    """A single real search_memory() call over 20 result points stays fast."""
    mock_qdrant = _wire_tools
    query_result = MagicMock()
    query_result.points = _make_points(20)
    mock_qdrant.query_points = AsyncMock(return_value=query_result)

    start = time.perf_counter()
    # Ask for all 20 — search_memory now slices the fused pool to the requested
    # limit, so the default (10) would otherwise cap the returned set.
    results = await search_memory("durable decision", limit=20)
    elapsed_ms = (time.perf_counter() - start) * 1000

    assert len(results) == 20
    assert all("recency" in r and "score" in r for r in results)
    assert elapsed_ms < MAX_SINGLE_SEARCH_MS, (
        f"search_memory() took {elapsed_ms:.1f} ms (limit {MAX_SINGLE_SEARCH_MS} ms)"
    )


async def test_search_memory_repeated_queries_throughput(_wire_tools):
    """20 consecutive real search_memory() calls stay within budget."""
    mock_qdrant = _wire_tools
    query_result = MagicMock()
    query_result.points = _make_points(20)
    mock_qdrant.query_points = AsyncMock(return_value=query_result)

    queries = ["estormi", "memory palace", "alice bob", "durable decision", "café"] * 4

    start = time.perf_counter()
    for q in queries:
        results = await search_memory(q)
        assert isinstance(results, list)
    elapsed_ms = (time.perf_counter() - start) * 1000

    assert elapsed_ms < MAX_BATCH_SEARCH_MS, (
        f"{len(queries)} search_memory() calls took {elapsed_ms:.1f} ms "
        f"(limit {MAX_BATCH_SEARCH_MS} ms)"
    )
