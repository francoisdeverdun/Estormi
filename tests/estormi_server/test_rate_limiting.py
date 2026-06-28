"""Integration test for the SlowAPI rate limiter.

``conftest.py`` disables the limiter suite-wide so per-test request counts
cannot flake limit-bounded tests. That leaves the limiter's *rejection*
behaviour uncovered — a regression that silently disabled rate limiting (a
real security control on every ``/api`` surface) would otherwise ship green.

This file opts back in for a single test and proves an over-budget request is
actually refused with HTTP 429.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.integration


@pytest.fixture
def rate_limiter_enabled():
    """Re-enable the shared limiter for one test, then disable it again.

    The conftest sets ``limiter.enabled = False`` for the whole run; this
    fixture flips it back on and restores it on teardown so no later test
    inherits an active limiter.
    """
    from estormi_server.server.limiter import limiter

    limiter.enabled = True
    try:
        yield limiter
    finally:
        limiter.enabled = False


async def test_search_memory_rejects_requests_over_budget(client, rate_limiter_enabled):
    """``/search_memory`` is capped at 120/minute — the 121st request gets 429.

    The exact count locks the configured budget: bumping the limit or dropping
    the ``@limiter.limit`` decorator both surface as a failure here.
    """
    for i in range(120):
        resp = await client.post("/search_memory", json={"query": "q"})
        assert resp.status_code == 200, f"request {i + 1} returned {resp.status_code}"

    over_budget = await client.post("/search_memory", json={"query": "q"})
    assert over_budget.status_code == 429
