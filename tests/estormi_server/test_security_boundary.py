"""Integration tests for the security boundary on the FastAPI app.

These tests cover the regressions surfaced in the v1.8 review:

* A request that looks loopback (client host 127.0.0.1) but carries the
  ``X-Estormi-Forwarded`` header MUST be treated as remote — defence-in-depth
  so a reverse proxy forwarding to loopback can't launder remote traffic into
  the loopback auth-skip.
* ``/api/open-url`` MUST refuse anything outside the closed allow-list — the
  prior implementation passed user-supplied URLs through to an ``osascript
  -e 'open location "<url>"'`` shell, allowing AppleScript injection.
* OpenAPI / Swagger surfaces (``/openapi.json``, ``/api-docs``) are disabled
  outright; the boundary still rejects them for a remote / proxied caller
  (the middleware runs before routing, so a missing route never leaks).
* State-changing ``/api/...`` requests MUST carry either a valid bearer or
  the ``X-Estormi-Origin`` header — a cross-origin browser page on loopback
  cannot forge a custom header without triggering CORS preflight.
* Every security-relevant decision MUST appear in the audit log.
"""

from __future__ import annotations

import importlib
import json
import os
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

pytestmark = pytest.mark.integration

PROXIED_HEADERS = {"X-Estormi-Forwarded": "1"}


def _read_audit_lines() -> list[dict]:
    path = Path(os.environ["AUDIT_LOG_PATH"])
    if not path.exists():
        return []
    out: list[dict] = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


@pytest.fixture
def fresh_audit_log(tmp_path, monkeypatch):
    """Point the audit module at a per-test file and reload it.

    The audit module opens its file handle lazily and caches it, so reloading
    both ``memory_core.settings`` (which read ``AUDIT_LOG_PATH`` at import)
    and ``memory_core.audit`` is enough to pick up the new path.
    """
    path = tmp_path / "audit.jsonl"
    monkeypatch.setenv("AUDIT_LOG_PATH", str(path))

    from memory_core import audit as core_audit
    from memory_core import settings as core_settings

    importlib.reload(core_settings)
    importlib.reload(core_audit)
    yield path


# ── X-Estormi-Forwarded breaks the loopback shortcut ────────────────────────


async def test_forwarded_loopback_request_needs_token():
    """Loopback TCP + forwarded header → treat as remote → 403 without token."""
    from httpx import ASGITransport, AsyncClient

    from estormi_server.main import app

    transport = ASGITransport(app=app, client=("127.0.0.1", 4321))
    with patch("keyring.get_password", return_value=""):
        async with AsyncClient(transport=transport, base_url="http://proxy") as ac:
            resp = await ac.get("/api/settings", headers=PROXIED_HEADERS)

    # No token configured → forwarded request gets 403 (the same response a
    # remote IP would get). The TCP-level 127.0.0.1 must NOT exempt it.
    assert resp.status_code == 403, resp.text


async def test_forwarded_loopback_request_with_token_succeeds():
    """Same as above but with a matching bearer → request goes through."""
    from httpx import ASGITransport, AsyncClient

    from estormi_server.main import app

    transport = ASGITransport(app=app, client=("127.0.0.1", 4321))
    headers = {
        **PROXIED_HEADERS,
        "Authorization": "Bearer s3cret",
    }
    with patch("keyring.get_password", return_value="s3cret"):
        async with AsyncClient(transport=transport, base_url="http://proxy") as ac:
            resp = await ac.get("/health", headers=headers)

    # /health is public regardless of auth — but the key signal is that the
    # middleware didn't 401 on the bearer comparison.
    assert resp.status_code == 200


# ── /openapi.json + /api-docs are disabled and boundary-rejected ───────────


async def test_openapi_blocked_for_forwarded_request_without_token():
    """A proxied client must not reach the schema. The endpoint is disabled
    (``openapi_url=None``), but the boundary rejects it before routing — so
    the proxied, token-less caller gets 403, never a 404 that confirms layout."""
    from httpx import ASGITransport, AsyncClient

    from estormi_server.main import app

    transport = ASGITransport(app=app, client=("127.0.0.1", 4321))
    with patch("keyring.get_password", return_value=""):
        async with AsyncClient(transport=transport, base_url="http://proxy") as ac:
            resp = await ac.get("/openapi.json", headers=PROXIED_HEADERS)

    assert resp.status_code == 403


# ── /api/open-url enforces a closed allow-list ──────────────────────────────


async def test_open_url_rejects_arbitrary_url(client):
    resp = await client.post("/api/open-url", json={"url": "https://example.com"})
    assert resp.status_code == 400
    assert "allow-list" in resp.json().get("detail", "")


async def test_open_url_rejects_applescript_injection_attempt(client):
    """Anything resembling the historical injection payload must be refused."""
    bad = 'x-apple.systempreferences:"&do shell script "open -a Calculator"&"'
    resp = await client.post("/api/open-url", json={"url": bad})
    assert resp.status_code == 400


async def test_open_url_rejects_non_string_url(client):
    resp = await client.post("/api/open-url", json={"url": ["nope"]})
    assert resp.status_code == 400


async def test_open_url_accepts_allowlisted_pane(client):
    """Allow-listed URLs invoke `open` (subprocess.run is patched)."""
    pane = "x-apple.systempreferences:com.apple.preference.security?Privacy_Automation"
    with patch("subprocess.run") as mock_run:
        resp = await client.post("/api/open-url", json={"url": pane})
    assert resp.status_code == 200
    mock_run.assert_called_once()
    # The literal URL — never user-controlled — is passed as argv to `open`.
    args, _ = mock_run.call_args
    assert args[0] == ["open", pane]


# ── /mcp tool errors no longer echo raw exception text ─────────────────────


async def test_tool_error_does_not_leak_raw_exception_message(client):
    """Raw exception text MUST NOT reach the client (it can carry paths/SQL)."""
    with patch(
        "estormi_server.api.mcp_rpc._search_memory",
        AsyncMock(side_effect=RuntimeError("/Users/secret/path/oops.db")),
    ):
        resp = await client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 99,
                "method": "tools/call",
                "params": {"name": "search_memory", "arguments": {"query": "q"}},
            },
        )
    data = resp.json()
    assert data["error"]["code"] == -32000
    assert "/Users/secret/path" not in data["error"]["message"]
    assert "oops.db" not in data["error"]["message"]


# ── CSRF gate on state-changing /api/... endpoints ──────────────────────────


async def test_csrf_rejects_post_without_origin_header(client):
    """POST/PUT/DELETE on /api/... without X-Estormi-Origin and no bearer → 403."""
    resp = await client.put(
        "/api/settings",
        json={"some_key": "v"},
        headers={"X-Estormi-Origin": "", "Authorization": ""},
    )
    assert resp.status_code == 403
    assert "X-Estormi-Origin" in resp.json().get("detail", "")


async def test_csrf_accepts_post_with_origin_header(client):
    """Same request with the header set → goes through."""
    resp = await client.put(
        "/api/settings",
        json={"some_key": "v"},
    )
    assert resp.status_code == 200


async def test_csrf_exempts_safe_methods(client):
    """GET on /api/... never needs the CSRF header."""
    resp = await client.get("/api/settings", headers={"X-Estormi-Origin": ""})
    assert resp.status_code == 200


async def test_csrf_gate_covers_unprefixed_ingest_shims(client):
    """The root-mounted destructive shims (/ingest_chunk, /ingest_delete) get the
    same CSRF header requirement as /api/* — they mutate the store but live
    outside the /api/ prefix. Header-less → 403; with the header → not a CSRF
    reject (the connectors send it via shared.http_client.post_chunk)."""
    for shim, body in (
        ("/ingest_chunk", {"text": "x", "source": "s", "source_id": "i"}),
        ("/ingest_delete", {"source": "s", "source_id": "i"}),
    ):
        rejected = await client.post(
            shim, json=body, headers={"X-Estormi-Origin": "", "Authorization": ""}
        )
        assert rejected.status_code == 403, f"{shim} should require X-Estormi-Origin"
        assert "X-Estormi-Origin" in rejected.json().get("detail", "")
        # The default `client` fixture sends the header → no CSRF 403.
        accepted = await client.post(shim, json=body)
        assert accepted.status_code != 403, f"{shim} with header must not be a CSRF reject"


# ── Audit log captures security-relevant decisions ──────────────────────────


async def test_audit_log_records_auth_failure(fresh_audit_log):
    """A bearer-mismatched remote request appends a reject entry."""
    from httpx import ASGITransport, AsyncClient

    from estormi_server.main import app

    transport = ASGITransport(app=app, client=("203.0.113.10", 4321))
    with patch("keyring.get_password", return_value=""):
        async with AsyncClient(transport=transport, base_url="http://remote") as ac:
            resp = await ac.get("/api/settings")
    assert resp.status_code == 403

    entries = _read_audit_lines()
    rejects = [
        e
        for e in entries
        if e.get("event") == "security_decision" and e.get("decision") == "reject"
    ]
    assert rejects, f"no security_decision rejects in {entries}"
    last = rejects[-1]
    assert last["path"] == "/api/settings"
    assert last["reason"] == "forwarded_without_token"
    # Client host MUST be hashed, never raw.
    assert "203.0.113.10" not in Path(os.environ["AUDIT_LOG_PATH"]).read_text()


async def test_audit_log_records_csrf_reject(fresh_audit_log, client):
    resp = await client.put(
        "/api/settings",
        json={"k": "v"},
        headers={"X-Estormi-Origin": "", "Authorization": ""},
    )
    assert resp.status_code == 403

    rejects = [
        e
        for e in _read_audit_lines()
        if e.get("event") == "security_decision" and e.get("decision") == "reject"
    ]
    assert any(e.get("reason") == "csrf_origin_missing" for e in rejects)


async def test_audit_log_records_settings_write(fresh_audit_log, client):
    resp = await client.put("/api/settings", json={"some_key": "v"})
    assert resp.status_code == 200

    accepts = [
        e
        for e in _read_audit_lines()
        if e.get("event") == "security_decision"
        and e.get("decision") == "accept"
        and e.get("path") == "/api/settings"
    ]
    assert accepts
    assert "settings_write" in accepts[-1]["reason"]


async def test_audit_log_records_open_url_reject(fresh_audit_log, client):
    resp = await client.post("/api/open-url", json={"url": "https://example.com"})
    assert resp.status_code == 400

    rejects = [
        e
        for e in _read_audit_lines()
        if e.get("event") == "security_decision"
        and e.get("decision") == "reject"
        and e.get("path") == "/api/open-url"
    ]
    assert rejects
    assert rejects[-1]["reason"] == "open_url_not_in_allowlist"


# ── Host-header validation defeats DNS rebinding (sweep 2 U9) ─────────────────
#
# ``_is_loopback_request`` used to grant the loopback auth-skip purely on the
# TCP peer being 127.0.0.1. A page on ``attacker.com`` whose DNS is rebound to
# 127.0.0.1 would then hit an unprefixed mutating shim (``/ingest_delete``) with
# the TCP peer at loopback and get the auth-skip, letting it read/destroy the
# local memory. The fix additionally requires the ``Host`` header to name an
# expected local host. (``/ingest_delete`` is now ALSO under the CSRF gate via
# ``_CSRF_PROTECTED_EXACT`` — these tests send the origin header so they isolate
# the Host-boundary behaviour under test.)


def _fake_rebind_request(client_host: str | None, host_header: str | None) -> object:
    """Minimal stand-in for a Starlette Request for ``_is_loopback_request``.

    Only ``.headers.get`` and ``.client.host`` are exercised by the function
    under test, so a SimpleNamespace with a tiny headers shim suffices.
    """
    from types import SimpleNamespace

    class _Headers:
        def __init__(self, mapping: dict[str, str]):
            self._m = {k.lower(): v for k, v in mapping.items()}

        def get(self, key: str, default: str = "") -> str:
            return self._m.get(key.lower(), default)

    headers: dict[str, str] = {}
    if host_header is not None:
        headers["host"] = host_header
    client = None if client_host is None else SimpleNamespace(host=client_host)
    return SimpleNamespace(headers=_Headers(headers), client=client)


def test_rebind_attacker_host_not_trusted():
    """Loopback TCP peer + Host: attacker.com → NOT loopback-trusted."""
    from estormi_server.server import security

    req = _fake_rebind_request("127.0.0.1", "attacker.com:8000")
    assert security._is_loopback_request(req) is False


def test_rebind_loopback_ip_host_trusted():
    from estormi_server.server import security

    req = _fake_rebind_request("127.0.0.1", "127.0.0.1:8000")
    assert security._is_loopback_request(req) is True


def test_rebind_localhost_host_trusted():
    from estormi_server.server import security

    req = _fake_rebind_request("127.0.0.1", "localhost:8000")
    assert security._is_loopback_request(req) is True


def test_rebind_ipv6_loopback_host_trusted():
    from estormi_server.server import security

    req = _fake_rebind_request("::1", "[::1]:8000")
    assert security._is_loopback_request(req) is True


def test_rebind_missing_host_header_rejected():
    """Empty/missing Host header is untrusted (prevents host-header bypass)."""
    from estormi_server.server import security

    req = _fake_rebind_request("127.0.0.1", None)
    assert security._is_loopback_request(req) is False


def test_rebind_named_test_client_peer_trusted():
    """ASGI test transport peer (client.host=testclient, Host=test)."""
    from estormi_server.server import security

    req = _fake_rebind_request("testclient", "test")
    assert security._is_loopback_request(req) is True


def test_rebind_configured_bind_host_trusted(monkeypatch):
    """A LAN hostname set via MCP_SERVER_HOST is honoured."""
    from estormi_server.server import security

    monkeypatch.setenv("MCP_SERVER_HOST", "memory.lan")
    req = _fake_rebind_request("127.0.0.1", "memory.lan:8000")
    assert security._is_loopback_request(req) is True


def test_rebind_unconfigured_lan_host_not_trusted(monkeypatch):
    from estormi_server.server import security

    monkeypatch.delenv("MCP_SERVER_HOST", raising=False)
    req = _fake_rebind_request("127.0.0.1", "memory.lan:8000")
    assert security._is_loopback_request(req) is False


async def _post_ingest_delete(host_header: str):
    from httpx import ASGITransport, AsyncClient

    from estormi_server.main import app

    # Loopback TCP peer, but a caller-controlled Host header (the rebind shape).
    transport = ASGITransport(app=app, client=("127.0.0.1", 4321))
    with patch("keyring.get_password", return_value=""):
        async with AsyncClient(transport=transport, base_url="http://x") as ac:
            return await ac.post(
                "/ingest_delete",
                json={"source": "s", "source_id": "i"},
                # Origin header present so this isolates the Host/rebind boundary
                # from the CSRF gate that now also covers /ingest_delete.
                headers={"Host": host_header, "X-Estormi-Origin": "tauri"},
            )


async def test_rebind_via_unprefixed_shim_rejected():
    """Host: attacker.com on the unprefixed /ingest_delete shim → rejected.

    Without the fix the loopback peer alone grants trust and the request is
    allowed (would reach routing); with the fix the untrusted Host denies the
    auth-skip and the boundary returns 401/403 before routing.
    """
    resp = await _post_ingest_delete("attacker.com:8000")
    assert resp.status_code in (401, 403), resp.text


async def test_rebind_loopback_host_baseline_allowed():
    """Same request with a loopback Host clears the boundary (not 401/403).

    No token configured + loopback-trusted → the security boundary lets it
    through to routing; the route itself may then succeed or 500 on missing
    deps, but it must NOT be a boundary 401/403 rejection.
    """
    resp = await _post_ingest_delete("127.0.0.1:8000")
    assert resp.status_code not in (401, 403), resp.text


# ── MCP auto-token protects /mcp even without user-configured token ──────────


async def test_mcp_auto_token_rejects_unauthenticated_loopback(client):
    """/mcp from loopback without bearer → 401 when auto-token is active."""
    from estormi_server.server import security

    security._cached_token = None
    security._token_resolved = True
    security._mcp_auto_token = "auto-secret-42"

    resp = await client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "ping", "params": {}},
        headers={"Authorization": ""},
    )
    assert resp.status_code == 401, resp.text
    assert "Bearer" in resp.headers.get("www-authenticate", "")


async def test_mcp_auto_token_accepts_correct_bearer(client):
    """/mcp with the auto-token bearer → passes the security boundary."""
    from estormi_server.server import security

    security._cached_token = None
    security._token_resolved = True
    security._mcp_auto_token = "auto-secret-42"

    resp = await client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "ping", "params": {}},
        headers={"Authorization": "Bearer auto-secret-42"},
    )
    # Not a 401 — the security boundary passed; route may succeed or error.
    assert resp.status_code != 401, resp.text


async def test_mcp_auto_token_persisted_to_disk(tmp_path):
    """_generate_and_persist_mcp_token writes a readable file."""
    from estormi_server.server import security

    with patch.object(security, "mcp_token_path", return_value=str(tmp_path / ".mcp_token")):
        token = security._generate_and_persist_mcp_token()

    path = tmp_path / ".mcp_token"
    assert path.exists()
    assert path.read_text() == token
    assert oct(path.stat().st_mode & 0o777) == "0o600"
