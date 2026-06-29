"""Behavioural tests for the WHOOP OAuth endpoints (``estormi_server/api/whoop_oauth.py``).

Every WHOOP auth boundary is mocked at ``estormi_ingestion.whoop.auth`` — these tests
exercise the route logic (the in-process state cache, the three status branches,
the consent-open flow, and the browser-redirect callback HTML) without touching
the network, a real browser, or the keyring.
"""

from __future__ import annotations

from unittest.mock import patch
from urllib.parse import urlsplit

import pytest

pytestmark = pytest.mark.integration


# The bounded/TTL state-cache behaviour is covered once, against the shared
# implementation, in ``tests/estormi_server/test_oauth_state.py``.


# ─── credentials upload ──────────────────────────────────────────────────────


async def test_credentials_upload_saves_stripped(client):
    with patch("estormi_ingestion.whoop.auth.save_client") as save:
        r = await client.post(
            "/api/whoop/credentials/upload",
            json={"client_id": "  cid  ", "client_secret": "  sec  "},
        )
    assert r.status_code == 200
    assert r.json() == {"ok": True}
    save.assert_called_once_with("cid", "sec")


async def test_credentials_upload_rejects_empty(client):
    r = await client.post(
        "/api/whoop/credentials/upload",
        json={"client_id": "", "client_secret": "x"},
    )
    assert r.status_code == 422


# ─── status ──────────────────────────────────────────────────────────────────


async def test_status_no_client_present(client):
    with (
        patch("estormi_ingestion.whoop.auth.client_present", return_value=False),
        patch("estormi_ingestion.whoop.auth.redirect_uri", return_value="http://localhost:8000/cb"),
    ):
        r = await client.get("/api/whoop/status")
    assert r.status_code == 200
    assert r.json() == {
        "client": False,
        "connected": False,
        "redirect_uri": "http://localhost:8000/cb",
    }


async def test_status_connected_when_token_refreshable(client):
    with (
        patch("estormi_ingestion.whoop.auth.client_present", return_value=True),
        patch("estormi_ingestion.whoop.auth.load_token", return_value={"access_token": "x"}),
        patch("estormi_ingestion.whoop.auth.get_access_token", return_value="live"),
        patch("estormi_ingestion.whoop.auth.redirect_uri", return_value="uri"),
    ):
        r = await client.get("/api/whoop/status")
    body = r.json()
    assert body["client"] is True
    assert body["connected"] is True


async def test_status_disconnected_when_refresh_dead(client):
    # A stored token whose refresh no longer works reads as connected=False.
    with (
        patch("estormi_ingestion.whoop.auth.client_present", return_value=True),
        patch("estormi_ingestion.whoop.auth.load_token", return_value={"refresh_token": "x"}),
        patch("estormi_ingestion.whoop.auth.get_access_token", return_value=None),
        patch("estormi_ingestion.whoop.auth.redirect_uri", return_value="uri"),
    ):
        r = await client.get("/api/whoop/status")
    assert r.json()["connected"] is False


async def test_status_disconnected_when_no_token(client):
    with (
        patch("estormi_ingestion.whoop.auth.client_present", return_value=True),
        patch("estormi_ingestion.whoop.auth.load_token", return_value=None),
        patch("estormi_ingestion.whoop.auth.redirect_uri", return_value="uri"),
    ):
        r = await client.get("/api/whoop/status")
    body = r.json()
    assert body["client"] is True
    assert body["connected"] is False


# ─── consent open ────────────────────────────────────────────────────────────


async def test_auth_open_success_registers_state(client):
    from estormi_server.api import whoop_oauth

    whoop_oauth._OAUTH_STATES.clear()
    with (
        patch(
            "estormi_ingestion.whoop.auth.build_authorization_url",
            return_value="https://api.prod.whoop.com/oauth/oauth2/auth?state=x",
        ),
        patch("subprocess.Popen") as popen,
    ):
        r = await client.post("/api/whoop/auth/open")
    assert r.status_code == 200
    body = r.json()
    assert body["opened"] is True
    assert urlsplit(body["url"]).hostname == "api.prod.whoop.com"
    # The minted state is registered so the callback can later validate it.
    assert body["state"] in whoop_oauth._OAUTH_STATES
    popen.assert_called_once()


async def test_auth_open_browser_failure_reports_error(client):
    with (
        patch("estormi_ingestion.whoop.auth.build_authorization_url", return_value="https://x"),
        patch("subprocess.Popen", side_effect=OSError("no open binary")),
    ):
        r = await client.post("/api/whoop/auth/open")
    assert r.status_code == 200
    body = r.json()
    assert body["opened"] is False
    assert body["url"] == "https://x"
    assert body["error"] == "could not open browser"


async def test_auth_open_missing_credentials_returns_400(client):
    with patch(
        "estormi_ingestion.whoop.auth.build_authorization_url", side_effect=FileNotFoundError
    ):
        r = await client.post("/api/whoop/auth/open")
    assert r.status_code == 400
    assert "not configured" in r.json()["detail"]


async def test_auth_open_unexpected_error_returns_500(client):
    with patch(
        "estormi_ingestion.whoop.auth.build_authorization_url", side_effect=RuntimeError("boom")
    ):
        r = await client.post("/api/whoop/auth/open")
    assert r.status_code == 500
    assert r.json()["detail"] == "oauth open error"


# ─── browser-redirect callback ───────────────────────────────────────────────


async def test_callback_error_param_returns_400_html(client):
    r = await client.get("/api/whoop/auth/callback", params={"error": "access_denied"})
    assert r.status_code == 400
    assert r.headers["content-type"].startswith("text/html")
    assert "didn&#39;t return an authorization code" in r.text or "authorization code" in r.text


async def test_callback_no_code_returns_400(client):
    r = await client.get("/api/whoop/auth/callback")
    assert r.status_code == 400


async def test_callback_unknown_state_returns_400(client):
    from estormi_server.api import whoop_oauth

    whoop_oauth._OAUTH_STATES.clear()
    r = await client.get("/api/whoop/auth/callback", params={"code": "abc", "state": "ghost"})
    assert r.status_code == 400
    assert "state mismatch" in r.text


async def test_callback_success_exchanges_and_consumes_state(client):
    from estormi_server.api import whoop_oauth

    whoop_oauth._OAUTH_STATES.clear()
    whoop_oauth._OAUTH_STATES.add("good")
    with patch("estormi_ingestion.whoop.auth.exchange_code") as exch:
        r = await client.get(
            "/api/whoop/auth/callback", params={"code": "authcode", "state": "good"}
        )
    assert r.status_code == 200
    assert "now connected to WHOOP" in r.text
    exch.assert_called_once_with("authcode")
    # State is single-use — consumed even on success.
    assert "good" not in whoop_oauth._OAUTH_STATES


async def test_callback_exchange_failure_returns_500_without_leaking(client):
    from estormi_server.api import whoop_oauth

    whoop_oauth._OAUTH_STATES.clear()
    whoop_oauth._OAUTH_STATES.add("good")
    with patch(
        "estormi_ingestion.whoop.auth.exchange_code",
        side_effect=RuntimeError("secret-bearing-detail"),
    ):
        r = await client.get(
            "/api/whoop/auth/callback", params={"code": "authcode", "state": "good"}
        )
    assert r.status_code == 500
    assert "oauth_exchange_failed" in r.text
    # The raw exception must never reach the browser — it can carry the secret.
    assert "secret-bearing-detail" not in r.text


# ─── disconnect ──────────────────────────────────────────────────────────────


async def test_auth_delete_calls_delete_token(client):
    with patch("estormi_ingestion.whoop.auth.delete_token") as dele:
        r = await client.delete("/api/whoop/auth")
    assert r.status_code == 200
    assert r.json() == {"ok": True}
    dele.assert_called_once()
