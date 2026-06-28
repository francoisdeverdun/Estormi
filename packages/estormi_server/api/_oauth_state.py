"""Bounded, TTL-pruned in-process cache of OAuth ``state`` nonces.

Shared by the Google-Calendar (:mod:`api.calendar_oauth`) and WHOOP
(:mod:`api.whoop_oauth`) authorization-code flows, which previously each kept a
byte-identical copy of this logic. A ``state`` only needs to survive the few
seconds between ``/auth/url`` (where it is minted) and ``/auth/callback`` (where
it is validated and consumed); a server restart in that window just makes the
user retry from the SPA, which mints a fresh one.

The cache is bounded so a misbehaving — or malicious — caller hitting
``/auth/url`` in a loop cannot grow it without limit: entries older than the TTL
are pruned on every insert, and if the map is still over capacity the oldest
entries are evicted.
"""

from __future__ import annotations

import time


class OAuthStateCache:
    """A bounded set of live OAuth ``state`` values with per-insert pruning."""

    def __init__(self, ttl_seconds: int = 10 * 60, max_entries: int = 100) -> None:
        self._states: dict[str, float] = {}
        self._ttl = ttl_seconds
        self._max = max_entries

    def add(self, state: str) -> None:
        """Register a freshly minted ``state``, pruning expired/excess entries."""
        now = time.time()
        # Prune expired entries first.
        expired = [s for s, ts in self._states.items() if now - ts > self._ttl]
        for s in expired:
            self._states.pop(s, None)
        self._states[state] = now
        # If still over cap, evict the oldest entries.
        if len(self._states) > self._max:
            for s, _ in sorted(self._states.items(), key=lambda kv: kv[1])[
                : len(self._states) - self._max
            ]:
                self._states.pop(s, None)

    def __contains__(self, state: str) -> bool:
        # TTL-aware on read too: pruning only happens in add(), so a state minted
        # without a later /auth/url call would otherwise stay valid past the
        # advertised TTL. (CSRF still holds regardless — the nonce is unguessable
        # and single-use — but membership must honour the documented lifetime.)
        ts = self._states.get(state)
        return ts is not None and (time.time() - ts) <= self._ttl

    def consume(self, state: str) -> None:
        """Drop a ``state`` after a callback validated it — single-use. Idempotent."""
        self._states.pop(state, None)

    def clear(self) -> None:
        """Drop every state (used by tests for isolation)."""
        self._states.clear()

    def __len__(self) -> int:
        return len(self._states)
