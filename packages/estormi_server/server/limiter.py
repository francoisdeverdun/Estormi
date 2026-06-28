"""Shared SlowAPI limiter instance.

The limiter is created once at import time so every router can decorate its
endpoints with ``@limiter.limit(...)`` while still sharing a single rate
budget. ``main.py`` wires it onto ``app.state.limiter`` and registers the
429 handler. Centralising it here also keeps test patching simple — there
is exactly one canonical ``limiter`` symbol in the codebase.
"""

from __future__ import annotations

from slowapi import Limiter
from slowapi.util import get_remote_address

# Keyed by remote address, but the server binds 127.0.0.1 only and every
# caller (Tauri webview, MCP clients, ingestion subprocesses) originates from
# loopback — so in practice all local traffic shares one global per-host
# bucket. This is by design for a single-user local app: the per-endpoint
# limits are tuned as global ceilings, not per-distinct-client quotas.
limiter = Limiter(key_func=get_remote_address)
