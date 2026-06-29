"""Shared ingestion-side configuration helpers.

Single source of truth for the MCP server URL the connectors POST chunks to.
Each connector used to inline ``os.getenv("MCP_SERVER_URL", …)`` with a default
that drifted between ``localhost`` and ``127.0.0.1`` — harmless on most boxes,
but a needless inconsistency. Centralising it pins one canonical default
(``127.0.0.1`` — loopback by literal IP, matching the server's own
``_resolve_self_url``) while still honouring the ``MCP_SERVER_URL`` override.
"""

from __future__ import annotations

import os

# Canonical loopback default. The server binds 127.0.0.1; using the literal IP
# (rather than ``localhost``) avoids a DNS round-trip and any /etc/hosts skew.
_DEFAULT_MCP_URL = "http://127.0.0.1:8000"


def mcp_url() -> str:
    """Return the MCP server base URL (``MCP_SERVER_URL`` env or the default).

    The trailing slash is stripped so callers can append paths
    (``f"{mcp_url()}/ingest_chunk"``) without doubling it.
    """
    return os.getenv("MCP_SERVER_URL", _DEFAULT_MCP_URL).rstrip("/")
