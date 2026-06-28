"""Request-level security boundary for the Estormi MCP server.

This module owns the loopback / bearer-token gate and the CSRF check that
sits in front of every request. Anything that changes the wire-level
contract must also be reflected in the audit trail emitted by
``memory_core.audit``.
"""

from __future__ import annotations

import asyncio
import ipaddress
import os
import secrets

import structlog
from fastapi import Request
from fastapi.responses import JSONResponse

from memory_core.settings import MCP_TOKEN_ENV_VARS

_log = structlog.get_logger()
_token_source_logged = False

# Cached bearer token. Resolved once at lifespan startup (see ``refresh_token_cache``)
# and reused for every request — keyring.get_password() on macOS can stall a
# few hundred ms and was previously called from the security_boundary middleware
# on every request, saturating the threadpool the engines also share.
# ``None`` = no token configured (env + keychain both empty); a non-empty string
# is the resolved token. ``_token_resolved`` disambiguates "no token" from "not
# looked up yet" so the empty result is cached too — otherwise every request in
# the common no-token local setup re-hits the keychain and stalls under load.
_cached_token: str | None = None
_token_resolved: bool = False

# ─── Public surfaces / header names ──────────────────────────────────────────

_PUBLIC_PATHS = {"/health", "/favicon.ico"}
_PUBLIC_PREFIXES = ("/brand/", "/source-icons/")
# Hosts treated as loopback even though they're not IPs. ``test*`` covers the
# ASGI test client (httpx ASGITransport reports ``client=("testclient",…)``).
# We deliberately do NOT include the empty string: ``request.client is None``
# happens for the ASGI lifespan path and a few edge cases; trusting it as
# loopback would grant a header-less context the same auth-skip as a real
# 127.0.0.1 caller. Real loopback requests always have a populated client.
_LOCAL_CLIENT_HOSTS = {"test", "testclient", "testserver", "localhost"}
# Defence-in-depth for any future reverse proxy: a request that carries this
# header is never granted the loopback auth-skip, even if it arrives on
# 127.0.0.1. Its presence can only REDUCE trust, never grant it, so an attacker
# gains nothing by forging it. No first-party caller sets it today.
_FORWARDED_HEADER = "X-Estormi-Forwarded"
# Set by the Tauri webview (and any first-party caller) on every state-changing
# request. The app registers NO CORS middleware, so a cross-origin browser page
# can't add a custom header (the preflight gets no Access-Control-Allow-* and
# fails by default) — the header's presence therefore proves the request came
# from a first-party context. This is an ADDITIONAL gate on top of the
# bearer/loopback check, not a replacement.
_CSRF_HEADER = "X-Estormi-Origin"
# Carve-outs for the CSRF check. Liveness lives at `/health` (no `/api/`
# prefix) and is mounted outside this middleware, so no exemption is
# needed today. Anything new that genuinely needs to be reachable from a
# plain form post (no wizard exists today — the first-launch flow was
# removed) MUST be added here with a comment explaining why.
_CSRF_EXEMPT_EXACT: set[str] = set()
_CSRF_EXEMPT_PREFIXES: tuple[str, ...] = ()
_CSRF_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
# Root-mounted state-changing shims that live OUTSIDE ``/api/`` but still mutate
# the store (chunk ingest/delete). They get the SAME CSRF header requirement as
# ``/api/*`` — the connectors send it via ``shared.http_client.post_chunk``.
# Deliberately NOT here: ``/mcp`` (external MCP clients authenticate with a bearer
# and are covered by the no-CORS preflight backstop for browsers; gating it would
# break tokenless local MCP clients) and the read-only POSTs ``/search_memory`` /
# ``/fetch_around`` (not state-changing).
_CSRF_PROTECTED_EXACT = {"/ingest_chunk", "/ingest_batch", "/ingest_delete"}


def _host_header_is_trusted(request: Request) -> bool:
    """Whether the ``Host`` header names an expected local host.

    Defence against DNS rebinding: a malicious page on ``attacker.com`` can be
    rebound to resolve to 127.0.0.1, so the TCP peer becomes loopback while the
    ``Host`` header still says ``attacker.com``. The IP-loopback auth-skip must
    therefore ALSO require the requested host to be one we recognise. Like the
    ``_FORWARDED_HEADER`` check, this can only REDUCE trust, never grant it.

    Trusted Host values:
      * the named ASGI/test client hosts in ``_LOCAL_CLIENT_HOSTS`` (the test
        transport sends ``Host: test`` / ``testserver``),
      * ``localhost`` and any loopback IP (``127.0.0.1``, ``::1``, …),
      * the configured bind host from ``MCP_SERVER_HOST`` (so an intentional
        LAN hostname/IP is honoured),
    Any other host — including an empty/missing one — is untrusted.
    """
    raw = request.headers.get("host", "")
    if not raw:
        return False
    host = raw.strip().lower()
    # Strip the optional ``:port``. For IPv6 the address is bracketed
    # (``[::1]:8000``); split the port off after the closing bracket so the
    # colons inside the address are preserved.
    if host.startswith("["):
        bracket = host.find("]")
        if bracket != -1:
            host = host[1:bracket]
        else:
            host = host.strip("[]")
    elif host.count(":") == 1:
        # ``hostname:port`` or ``127.0.0.1:port`` — a bare IPv6 literal would
        # have more than one colon, so a single colon is always a port.
        host = host.rsplit(":", 1)[0]
    host = host.strip("[]")
    if host in _LOCAL_CLIENT_HOSTS:
        return True
    configured = (os.getenv("MCP_SERVER_HOST") or "").strip().lower()
    if configured and host == configured:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _is_loopback_request(request: Request) -> bool:
    # A request that claims to have been forwarded is NEVER loopback for trust
    # purposes, even though TCP-level it originates on 127.0.0.1.
    if request.headers.get(_FORWARDED_HEADER):
        return False
    if request.client is None:
        # No transport-level client info — refuse to grant loopback trust.
        return False
    host = request.client.host
    # Named ASGI/test transport peers (client.host is "testclient"/"testserver")
    # stay trusted directly — that's the transport peer, separate from the Host
    # header checked below for real IP-loopback callers.
    if host in _LOCAL_CLIENT_HOSTS:
        return True
    try:
        is_ip_loopback = ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False
    # Real IP-loopback peer: additionally require the Host header to name an
    # expected local host, defeating DNS rebinding (attacker.com → 127.0.0.1).
    return is_ip_loopback and _host_header_is_trusted(request)


async def _resolve_bearer_token() -> str:
    """Look up the configured bearer token from env or keychain.

    The keyring round-trip stays slow on macOS — callers should go through
    ``_configured_bearer_token`` which serves the cached value populated at
    lifespan startup.
    """
    global _token_source_logged
    source = ""
    token = ""
    for env_name in MCP_TOKEN_ENV_VARS:
        candidate = (os.getenv(env_name) or "").strip()
        if candidate:
            token = candidate
            source = env_name
            break
    if not token:
        try:
            import keyring  # noqa: PLC0415

            token = (
                await asyncio.to_thread(lambda: keyring.get_password("estormi", "mcp_token") or "")
            ).strip()
            if token:
                source = "keyring"
        except Exception as exc:
            _log.warning("security.keyring_lookup_failed", error=str(exc))
            token = ""
    if token and source and not _token_source_logged:
        _token_source_logged = True
        _log.info("security.bearer_token_source", source=source)
    return token


_MCP_TOKEN_FILENAME = ".mcp_token"

# Auto-generated per-launch token protecting /mcp and /sse when no user-
# configured bearer token exists.  Written to DATA_DIR/.mcp_token (chmod 600)
# so that MCP clients (Claude Desktop, etc.) can read it from a known path.
_mcp_auto_token: str | None = None


def mcp_token_path() -> str:
    """Return the on-disk path where the MCP auto-token is persisted."""
    from memory_core.settings import DATA_DIR  # noqa: PLC0415

    return os.path.join(DATA_DIR, _MCP_TOKEN_FILENAME)


def _generate_and_persist_mcp_token() -> str:
    """Create a per-launch random bearer token for MCP/SSE endpoints."""
    global _mcp_auto_token
    token = secrets.token_urlsafe(32)
    _mcp_auto_token = token
    path = mcp_token_path()
    try:
        with open(path, "w") as f:
            f.write(token)
        os.chmod(path, 0o600)
    except OSError:
        _log.warning("security.mcp_token_write_failed", path=path, exc_info=True)
    _log.info("security.mcp_auto_token_generated", path=path)
    return token


async def refresh_token_cache() -> str:
    """Force a re-read of the bearer token (env then keychain) and update
    the cache. Called at lifespan startup so subsequent requests skip the slow
    keychain round-trip.

    When no user-configured token is found, a per-launch random token is
    generated for ``/mcp`` and ``/sse`` and persisted to
    ``DATA_DIR/.mcp_token``.  Non-MCP loopback requests remain open.
    """
    global _cached_token, _token_resolved
    resolved = await _resolve_bearer_token()
    _cached_token = resolved or None
    _token_resolved = True
    if not resolved:
        _generate_and_persist_mcp_token()
    return resolved


async def _configured_bearer_token() -> str:
    if _token_resolved:
        return _cached_token or ""
    return await refresh_token_cache()


def _has_valid_bearer(request: Request, expected_token: str) -> bool:
    auth = request.headers.get("authorization", "")
    scheme, _, supplied = auth.partition(" ")
    if scheme.lower() != "bearer" or not supplied:
        return False
    return secrets.compare_digest(supplied.strip(), expected_token)


async def security_boundary(request: Request, call_next):
    from memory_core.audit import log_security_decision  # noqa: PLC0415

    _MAX_BODY_BYTES = 10 * 1024 * 1024  # 10 MB
    cl = request.headers.get("content-length")
    if cl and cl.isdigit() and int(cl) > _MAX_BODY_BYTES:
        return JSONResponse({"detail": "Request body too large"}, status_code=413)

    path = request.url.path
    client_host = request.client.host if request.client else ""
    if path in _PUBLIC_PATHS or any(path.startswith(prefix) for prefix in _PUBLIC_PREFIXES):
        return await call_next(request)

    token = await _configured_bearer_token()
    has_token = bool(token and _has_valid_bearer(request, token))
    is_loopback = _is_loopback_request(request)

    # /mcp and /sse ALWAYS require a bearer — either the user-configured one
    # or the per-launch auto-token.  This prevents any local process from
    # injecting/deleting chunks via the MCP transport without auth.
    if path in {"/mcp", "/sse"}:
        mcp_tok = token or _mcp_auto_token
        if mcp_tok and not (has_token or _has_valid_bearer(request, mcp_tok)):
            log_security_decision(
                decision="reject",
                path=path,
                client_host=client_host,
                reason="bearer_required_mcp",
                method=request.method,
            )
            return JSONResponse(
                {"detail": "Bearer token required"},
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )

    if not is_loopback and not has_token:
        status_code = 401 if token else 403
        detail = (
            "Bearer token required" if token else "Remote access requires a configured bearer token"
        )
        log_security_decision(
            decision="reject",
            path=path,
            client_host=client_host,
            reason="bearer_required_remote" if token else "forwarded_without_token",
            method=request.method,
        )
        return JSONResponse(
            {"detail": detail},
            status_code=status_code,
            headers={"WWW-Authenticate": "Bearer"} if token else None,
        )

    # CSRF gate: any state-changing /api/... request — plus the root-mounted
    # ingest shims in _CSRF_PROTECTED_EXACT — must either carry a valid bearer
    # (server-to-server / MCP clients) or the X-Estormi-Origin header (first-party
    # Tauri webview / connectors / curl from the local user). With no CORS
    # middleware configured, a cross-origin browser page on loopback can't add a
    # custom header — its preflight fails by default. GET/HEAD/OPTIONS are
    # exempt because they are not state-changing.
    if (
        request.method not in _CSRF_SAFE_METHODS
        and (path.startswith("/api/") or path in _CSRF_PROTECTED_EXACT)
        and not has_token
        and path not in _CSRF_EXEMPT_EXACT
        and not any(path.startswith(p) for p in _CSRF_EXEMPT_PREFIXES)
    ):
        if not (request.headers.get(_CSRF_HEADER) or "").strip():
            log_security_decision(
                decision="reject",
                path=path,
                client_host=client_host,
                reason="csrf_origin_missing",
                method=request.method,
            )
            return JSONResponse(
                {"detail": "Missing X-Estormi-Origin header"},
                status_code=403,
            )

    return await call_next(request)
