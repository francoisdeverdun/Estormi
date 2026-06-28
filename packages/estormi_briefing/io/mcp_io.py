"""MCP HTTP helpers — the briefing engine's read path into the chunk store.

Two thin POST wrappers the briefing uses to read memory back from the running
MCP server: ``_search_mcp_memory`` (semantic search) and ``_fetch_around_mcp``
(time-window bundle). Both are best-effort — any transport failure logs and
returns ``[]`` so a momentary server hiccup degrades a section rather than
aborting the whole run.
"""

from __future__ import annotations

import httpx
import structlog

from estormi_ingestion.shared.config import mcp_url

log = structlog.get_logger()

MCP_SERVER_URL = mcp_url()

# First-party origin marker. /search_memory and /fetch_around are root-mounted,
# so they aren't behind the CSRF gate (which only covers /api/* — see
# estormi_server/server/security.py); the header is harmless defense-in-depth.
_ORIGIN_HEADERS = {"X-Estormi-Origin": "estormi-knowledge"}


async def _post_results(path: str, payload: dict, timeout: float, what: str) -> list[dict]:
    """POST ``payload`` to a root-mounted MCP endpoint; ``[]`` on any failure.

    Normalises both response shapes the endpoints emit (a bare list, or a
    ``{"results": [...]}`` envelope).
    """
    try:
        async with httpx.AsyncClient(headers=_ORIGIN_HEADERS) as client:
            r = await client.post(f"{MCP_SERVER_URL}{path}", json=payload, timeout=timeout)
            if r.status_code == 200:
                data = r.json()
                return data if isinstance(data, list) else data.get("results", [])
            # A non-2xx is a contract mismatch (e.g. a window_days that exceeds
            # the endpoint cap → 422), not a transient hiccup. Log it loudly so
            # it can't silently degrade a briefing section to empty, the way the
            # 75-day-horizon vs 30-day-cap mismatch once did.
            log.warning("%s: HTTP %d — %s", what, r.status_code, r.text[:200])
    except Exception as exc:
        log.warning("%s: %s", what, exc)
    return []


async def _search_mcp_memory(payload: dict, timeout: float = 10.0) -> list[dict]:
    return await _post_results(
        "/search_memory", payload, timeout, "Could not fetch MCP memory context"
    )


async def _fetch_around_mcp(payload: dict, timeout: float = 12.0) -> list[dict]:
    """POST the MCP ``/fetch_around`` time-window endpoint; ``[]`` on failure."""
    return await _post_results(
        "/fetch_around", payload, timeout, "Could not fetch time-window bundle"
    )
