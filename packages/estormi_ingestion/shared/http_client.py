"""Shared HTTP client for connectors POSTing to the MCP server.

Wraps ``httpx.post`` with exponential backoff on connection errors and 5xx
responses. Connectors that previously fired a bare ``httpx.post`` were
losing whole chunks to a single hiccup on the loopback uvicorn — this
gives every source the same retry contract for free.

Keep this dependency-light: it must work inside the Python heredocs that
each shell-based connector pipes into the bundled standalone Python.
"""

from __future__ import annotations

import random
import time
from typing import Any

import httpx

_RETRYABLE_EXC = (
    httpx.ConnectError,
    httpx.ReadError,
    httpx.RemoteProtocolError,
    httpx.TimeoutException,
)


def _backoff_with_jitter(base: float, attempt: int) -> float:
    """Exponential backoff with full jitter, capped at 30s.

    Jitter matters here: a thousand parallel connectors hitting a 429 all
    back off the same amount and stampede the server again at the same
    instant. ``random.uniform(0, exp)`` spreads retries across the window.
    """
    exp = min(base * (2**attempt), 30.0)
    return random.uniform(0, exp)


def post_chunk(
    url: str,
    data: dict[str, Any],
    *,
    timeout: float = 60,
    retries: int = 6,
    backoff: float = 1.0,
    headers: dict[str, str] | None = None,
) -> httpx.Response:
    """POST ``data`` as JSON to ``url`` with retry on transient failures.

    Retries up to ``retries`` times on connection-class errors, timeouts,
    HTTP 5xx, and HTTP 429 responses. Backoff is exponential with full
    jitter (cap 30s/attempt); the default of 6 retries gives ~60s of
    cumulative wait worst-case, enough to outlast a 1-minute sliding
    rate-limit window. Returns the final ``httpx.Response`` (which may
    still be non-2xx — the caller decides how to react).
    """
    response: httpx.Response | None = None
    for attempt in range(retries + 1):
        try:
            response = httpx.post(
                url,
                json=data,
                headers={
                    "Content-Type": "application/json",
                    # First-party origin marker so the server's CSRF gate accepts
                    # the root-mounted ingest shims (/ingest_chunk, /ingest_delete)
                    # the same way it gates /api/*. A cross-origin browser page
                    # can't set a custom header (its preflight fails with no CORS
                    # middleware), so its presence proves a first-party caller. A
                    # caller-supplied header overrides this default.
                    "X-Estormi-Origin": "estormi-connector",
                    **(headers or {}),
                },
                timeout=timeout,
            )
        except _RETRYABLE_EXC:
            if attempt >= retries:
                raise
            time.sleep(_backoff_with_jitter(backoff, attempt))
            continue

        # Retry on 429 (server rate limiter) and 5xx; surface other 4xx to
        # caller (they're not transient). On 429, honor Retry-After if the
        # server provided one — slowapi sets it to the remaining window —
        # but add jitter on top so parallel callers don't stampede.
        if response.status_code == 429 and attempt < retries:
            retry_after = response.headers.get("Retry-After")
            try:
                hint = float(retry_after) if retry_after else 0.0
            except ValueError:
                hint = 0.0
            wait = max(hint, _backoff_with_jitter(backoff, attempt))
            time.sleep(min(max(wait, 0.5), 30.0))
            continue
        if response.status_code >= 500 and attempt < retries:
            time.sleep(_backoff_with_jitter(backoff, attempt))
            continue
        return response

    # Unreachable in practice: every attempt either returns inside the loop
    # (success, non-retryable, or a retryable status on the final attempt whose
    # ``attempt < retries`` guard fails) or re-raises. Kept to satisfy the
    # ``-> httpx.Response`` return type; ``response`` is always bound (the loop
    # runs at least once).
    assert response is not None
    return response


def post_batch(
    url: str,
    chunks: list[dict[str, Any]],
    *,
    timeout: float = 120,
    retries: int = 6,
    backoff: float = 1.0,
    headers: dict[str, str] | None = None,
) -> httpx.Response:
    return post_chunk(
        url,
        {"chunks": chunks},
        timeout=timeout,
        retries=retries,
        backoff=backoff,
        headers=headers,
    )
