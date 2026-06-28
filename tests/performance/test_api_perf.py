"""Performance benchmark: real FastAPI endpoint latency.

Drives genuine HTTP round-trips through the Estormi FastAPI app via the
shared ``client`` fixture (ASGI transport, mocked embedder/Qdrant — see
``tests/conftest.py``). A regression in routing, middleware, the security
boundary, or request handling would surface here as a blown time budget.

Each timed assertion takes the minimum of ``RUNS`` measurements so a single
GC pause / scheduler stutter on a shared CI runner does not flake the test.
We assert against the minimum (best-case), with the ceiling sized for the
slowest realistic run — only a true handler regression should trip it.
"""

from __future__ import annotations

import time

import pytest

pytestmark = pytest.mark.performance

# Generous ceilings — these run on shared CI runners with an in-process ASGI
# transport, so they are not measuring network latency. The bounds only trip
# on a real handler/middleware regression, not on runner jitter.
MAX_HEALTH_MS = 250
MAX_SEARCH_MS = 750
MAX_BATCH_HEALTH_MS = 4000

# Number of repeated measurements per benchmark; we keep the fastest one
# to suppress noise from GC pauses and CI scheduler jitter.
RUNS = 5


async def test_health_endpoint_latency(client):
    """A real GET /health round-trip through the app stays responsive."""
    samples: list[float] = []
    for _ in range(RUNS):
        start = time.perf_counter()
        resp = await client.get("/health")
        samples.append((time.perf_counter() - start) * 1000)
        assert resp.status_code == 200
        assert resp.json()["status"] in ("ok", "degraded")

    best_ms = min(samples)
    assert best_ms < MAX_HEALTH_MS, (
        f"GET /health best of {RUNS} took {best_ms:.1f} ms "
        f"(limit {MAX_HEALTH_MS} ms; samples={samples})"
    )


async def test_search_memory_endpoint_latency(client, mock_qdrant):
    """A real POST /search_memory round-trip (mocked backend) stays responsive."""
    samples: list[float] = []
    for _ in range(RUNS):
        start = time.perf_counter()
        resp = await client.post("/search_memory", json={"query": "estormi memory palace"})
        samples.append((time.perf_counter() - start) * 1000)
        assert resp.status_code == 200
        assert isinstance(resp.json(), list)

    best_ms = min(samples)
    assert best_ms < MAX_SEARCH_MS, (
        f"POST /search_memory best of {RUNS} took {best_ms:.1f} ms "
        f"(limit {MAX_SEARCH_MS} ms; samples={samples})"
    )


async def test_repeated_health_requests_throughput(client):
    """50 consecutive real /health round-trips stay within budget."""
    samples: list[float] = []
    for _ in range(RUNS):
        start = time.perf_counter()
        for _ in range(50):
            resp = await client.get("/health")
            assert resp.status_code == 200
        samples.append((time.perf_counter() - start) * 1000)

    best_ms = min(samples)
    assert best_ms < MAX_BATCH_HEALTH_MS, (
        f"50 GET /health best of {RUNS} took {best_ms:.1f} ms "
        f"(limit {MAX_BATCH_HEALTH_MS} ms; samples={samples})"
    )
