"""Unit tests for the shared OAuth state cache (``estormi_server/api/_oauth_state.py``).

The bounded/TTL nonce cache backs both the Google-Calendar and WHOOP OAuth
flows; this is the single home for its behaviour (add / prune / evict / membership
/ single-use consume), so neither route module re-tests it.
"""

from __future__ import annotations

import time

import pytest

pytestmark = pytest.mark.unit


def test_add_then_contains_and_consume():
    from estormi_server.api._oauth_state import OAuthStateCache

    cache = OAuthStateCache()
    cache.add("s1")
    assert "s1" in cache
    assert "missing" not in cache
    # consume is single-use and idempotent.
    cache.consume("s1")
    assert "s1" not in cache
    cache.consume("s1")  # no error on a second consume
    assert len(cache) == 0


def test_add_prunes_expired_entries():
    from estormi_server.api._oauth_state import OAuthStateCache

    cache = OAuthStateCache(ttl_seconds=600)
    # Inject an entry older than the TTL, then a fresh add must evict it.
    cache._states["stale"] = time.time() - 601
    cache.add("fresh")
    # len pins add()-side pruning independently of the read-side TTL guard: the
    # stale entry must be physically removed, not merely hidden on read.
    assert len(cache) == 1
    assert "stale" not in cache
    assert "fresh" in cache


def test_contains_enforces_ttl_on_read_without_intervening_add():
    # Membership must honour the TTL even when no later add() prunes the map —
    # otherwise a minted-but-abandoned state stays valid past its lifetime.
    from estormi_server.api._oauth_state import OAuthStateCache

    cache = OAuthStateCache(ttl_seconds=600)
    cache.add("s")
    assert "s" in cache
    cache._states["s"] = time.time() - 601  # age it past the TTL
    assert "s" not in cache


def test_add_enforces_max_bound_evicting_oldest():
    from estormi_server.api._oauth_state import OAuthStateCache

    cache = OAuthStateCache(max_entries=5)
    for i in range(15):
        cache.add(f"s{i}")
    assert len(cache) == 5
    # The most recently inserted state survives; the oldest are evicted.
    assert "s14" in cache
    assert "s0" not in cache


def test_clear_drops_everything():
    from estormi_server.api._oauth_state import OAuthStateCache

    cache = OAuthStateCache()
    cache.add("a")
    cache.add("b")
    cache.clear()
    assert len(cache) == 0
    assert "a" not in cache
