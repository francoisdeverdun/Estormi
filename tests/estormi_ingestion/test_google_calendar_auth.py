"""Behaviour tests for ``estormi_ingestion/google_calendar/auth.py``.

The OAuth helpers are security-sensitive, so these tests exercise the
observable effects (round-trips, persisted state, mapping shapes) and the
error branches (missing secrets, refresh failure, revoked token) with every
external boundary mocked — keyring, ``google_auth_oauthlib`` ``Flow``,
``google`` ``Credentials``, and ``httpx``. No test touches the network or the
real system keyring; token/secrets files live under ``tmp_path`` via
``ESTORMI_DATA_DIR``.

``keyring``, ``Flow``, ``Credentials`` and ``GRequest`` are all imported
*lazily inside the functions*, so patching the source modules
(``keyring.get_password``, ``google_auth_oauthlib.flow.Flow``,
``google.oauth2.credentials.Credentials``) is what the lazy ``import`` resolves
to. ``httpx`` is a module-level import, so it is patched as ``auth.httpx``.
"""

from __future__ import annotations

import json
import os
import sys
import types
from unittest.mock import MagicMock, patch

import pytest

from estormi_ingestion.google_calendar import auth

pytestmark = pytest.mark.unit


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    """Point the data-dir resolver at an isolated tmp directory."""
    monkeypatch.setenv("ESTORMI_DATA_DIR", str(tmp_path))
    return tmp_path


@pytest.fixture
def google_flow():
    """Install a stub ``google_auth_oauthlib.flow`` so the lazy
    ``from google_auth_oauthlib.flow import Flow`` resolves.

    The real package is not installed in the test venv (it is a runtime-only
    dependency), so the import would otherwise raise ``ModuleNotFoundError``
    before our patch could take effect. Returns the ``Flow`` mock.
    """
    flow_mod = types.ModuleType("google_auth_oauthlib.flow")
    flow_mod.Flow = MagicMock(name="Flow")
    pkg = types.ModuleType("google_auth_oauthlib")
    pkg.flow = flow_mod
    with patch.dict(
        sys.modules,
        {"google_auth_oauthlib": pkg, "google_auth_oauthlib.flow": flow_mod},
    ):
        yield flow_mod.Flow


@pytest.fixture
def google_creds():
    """Install stub ``google.oauth2.credentials`` and
    ``google.auth.transport.requests`` modules for the lazy imports in
    ``get_credentials``. Returns the ``Credentials`` mock.
    """
    creds_mod = types.ModuleType("google.oauth2.credentials")
    creds_mod.Credentials = MagicMock(name="Credentials")
    oauth2_pkg = types.ModuleType("google.oauth2")
    oauth2_pkg.credentials = creds_mod
    req_mod = types.ModuleType("google.auth.transport.requests")
    req_mod.Request = MagicMock(name="Request")
    transport_pkg = types.ModuleType("google.auth.transport")
    transport_pkg.requests = req_mod
    auth_pkg = types.ModuleType("google.auth")
    auth_pkg.transport = transport_pkg
    google_pkg = types.ModuleType("google")
    google_pkg.oauth2 = oauth2_pkg
    google_pkg.auth = auth_pkg
    with patch.dict(
        sys.modules,
        {
            "google": google_pkg,
            "google.oauth2": oauth2_pkg,
            "google.oauth2.credentials": creds_mod,
            "google.auth": auth_pkg,
            "google.auth.transport": transport_pkg,
            "google.auth.transport.requests": req_mod,
        },
    ):
        yield creds_mod.Credentials


# ─── Path resolution ────────────────────────────────────────────────────────


def test_token_file_resolves_under_data_dir(data_dir):
    assert auth._token_file() == str(data_dir / ".gcal_token")


def test_legacy_client_secrets_path_resolves_under_data_dir(data_dir):
    assert auth._legacy_client_secrets_path() == str(data_dir / "google_client_secrets.json")


# ─── Token storage round-trip ───────────────────────────────────────────────


def test_save_token_uses_keyring_when_available(data_dir):
    tok = {"token": "abc", "refresh_token": "r1"}
    with patch("keyring.set_password") as set_pw:
        auth.save_token(tok)
    set_pw.assert_called_once_with(auth.SERVICE_NAME, auth.TOKEN_KEY, json.dumps(tok))
    # Keyring succeeded, so no file fallback should have been written.
    assert not os.path.exists(auth._token_file())


def test_load_token_reads_from_keyring(data_dir):
    tok = {"token": "abc", "scopes": ["x"]}
    with patch("keyring.get_password", return_value=json.dumps(tok)) as get_pw:
        assert auth.load_token() == tok
    get_pw.assert_called_once_with(auth.SERVICE_NAME, auth.TOKEN_KEY)


def test_save_then_load_round_trip_via_keyring(data_dir):
    """A real round-trip with an in-memory keyring substitute."""
    store: dict = {}

    def fake_set(service, key, value):
        store[(service, key)] = value

    def fake_get(service, key):
        return store.get((service, key))

    tok = {"token": "t", "refresh_token": "r", "scopes": ["s"]}
    with (
        patch("keyring.set_password", side_effect=fake_set),
        patch("keyring.get_password", side_effect=fake_get),
    ):
        auth.save_token(tok)
        assert auth.load_token() == tok


def test_save_token_falls_back_to_file_when_keyring_raises(data_dir):
    tok = {"token": "filetoken"}
    with patch("keyring.set_password", side_effect=RuntimeError("no keyring")):
        auth.save_token(tok)
    path = auth._token_file()
    assert os.path.exists(path)
    with open(path, encoding="utf-8") as f:
        assert json.load(f) == tok
    # No leftover temp file from the atomic write.
    assert not os.path.exists(f"{path}.tmp")
    # chmod-600 was requested.
    assert oct(os.stat(path).st_mode & 0o777) == "0o600"


def test_load_token_falls_back_to_file_when_keyring_empty(data_dir):
    tok = {"token": "ondisk"}
    path = auth._token_file()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(tok, f)
    with patch("keyring.get_password", return_value=None):
        assert auth.load_token() == tok


def test_load_token_returns_none_when_absent_everywhere(data_dir):
    with patch("keyring.get_password", return_value=None):
        assert auth.load_token() is None


def test_load_token_handles_unreadable_file(data_dir):
    path = auth._token_file()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write("{ not json")
    with patch("keyring.get_password", return_value=None):
        assert auth.load_token() is None


# ─── Token deletion / revocation ────────────────────────────────────────────


def test_delete_token_revokes_and_clears_keyring_and_file(data_dir):
    # Token present on disk so delete has something to revoke + remove.
    path = auth._token_file()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"token": "live"}, f)

    with (
        patch("keyring.get_password", return_value=None),
        patch("keyring.delete_password") as del_pw,
        patch.object(auth, "httpx") as mock_httpx,
    ):
        auth.delete_token()

    mock_httpx.post.assert_called_once()
    # Revoke hit Google's revoke endpoint with the live token.
    _, kwargs = mock_httpx.post.call_args
    assert kwargs["params"] == {"token": "live"}
    del_pw.assert_called_once_with(auth.SERVICE_NAME, auth.TOKEN_KEY)
    assert not os.path.exists(path)


def test_delete_token_skips_revoke_when_no_token(data_dir):
    with (
        patch("keyring.get_password", return_value=None),
        patch("keyring.delete_password"),
        patch.object(auth, "httpx") as mock_httpx,
    ):
        auth.delete_token()
    mock_httpx.post.assert_not_called()


def test_delete_token_swallows_revoke_failure(data_dir):
    with (
        patch("keyring.get_password", return_value=json.dumps({"token": "x"})),
        patch("keyring.delete_password"),
        patch.object(auth, "httpx") as mock_httpx,
    ):
        mock_httpx.post.side_effect = RuntimeError("network down")
        # Must not raise despite the revoke failing.
        auth.delete_token()
    mock_httpx.post.assert_called_once()


# ─── Client secrets loading ─────────────────────────────────────────────────


def test_load_client_secrets_migrates_and_unwraps_installed_block(data_dir):
    # A legacy cleartext file is imported into the keyring on first load and
    # unwrapped from its ``installed`` envelope.
    payload = {"installed": {"client_id": "cid", "client_secret": "sec"}}
    with open(auth._legacy_client_secrets_path(), "w", encoding="utf-8") as f:
        json.dump(payload, f)
    assert auth._load_client_secrets() == payload["installed"]
    assert not os.path.exists(auth._legacy_client_secrets_path())


def test_load_client_secrets_migrates_and_unwraps_web_block(data_dir):
    payload = {"web": {"client_id": "cid"}}
    with open(auth._legacy_client_secrets_path(), "w", encoding="utf-8") as f:
        json.dump(payload, f)
    assert auth._load_client_secrets() == payload["web"]


def test_load_client_secrets_returns_bare_dict_when_unwrapped(data_dir):
    payload = {"client_id": "cid"}
    with open(auth._legacy_client_secrets_path(), "w", encoding="utf-8") as f:
        json.dump(payload, f)
    assert auth._load_client_secrets() == payload


def test_save_client_secrets_round_trips_via_keyring(data_dir):
    # Stored unwrapped in the keyring; readable back without any cleartext file.
    auth.save_client_secrets({"installed": {"client_id": "cid", "client_secret": "sec"}})
    assert auth._load_client_secrets() == {"client_id": "cid", "client_secret": "sec"}
    assert not os.path.exists(auth._legacy_client_secrets_path())


def test_load_client_secrets_raises_when_missing(data_dir):
    with pytest.raises(FileNotFoundError):
        auth._load_client_secrets()


# ─── Redirect URI ───────────────────────────────────────────────────────────


def test_redirect_uri_default_port(monkeypatch):
    monkeypatch.delenv("MCP_SERVER_PORT", raising=False)
    assert auth._redirect_uri() == "http://localhost:8000/api/calendar/auth/callback"


def test_redirect_uri_honours_env_port(monkeypatch):
    monkeypatch.setenv("MCP_SERVER_PORT", "9123")
    assert auth._redirect_uri() == "http://localhost:9123/api/calendar/auth/callback"


# ─── Credentials helpers ────────────────────────────────────────────────────


def test_creds_to_dict_maps_all_fields():
    creds = MagicMock()
    creds.token = "t"
    creds.refresh_token = "r"
    creds.token_uri = "uri"
    creds.client_id = "cid"
    creds.client_secret = "sec"
    creds.scopes = ["a", "b"]
    out = auth._creds_to_dict(creds)
    assert out == {
        "token": "t",
        "refresh_token": "r",
        "token_uri": "uri",
        "client_id": "cid",
        "client_secret": "sec",
        "scopes": ["a", "b"],
    }


def test_creds_to_dict_defaults_scopes_when_none():
    creds = MagicMock()
    creds.token = "t"
    creds.refresh_token = "r"
    creds.token_uri = "uri"
    creds.client_id = "cid"
    creds.client_secret = "sec"
    creds.scopes = None
    out = auth._creds_to_dict(creds)
    assert out["scopes"] == auth.SCOPES


# ─── get_credentials ────────────────────────────────────────────────────────


def test_get_credentials_returns_none_without_token(data_dir):
    with patch("keyring.get_password", return_value=None):
        assert auth.get_credentials() is None


def test_get_credentials_builds_valid_creds(data_dir, google_creds):
    tok = {
        "token": "t",
        "refresh_token": "r",
        "token_uri": "uri",
        "client_id": "cid",
        "client_secret": "sec",
        "scopes": ["s"],
    }
    fake_creds = MagicMock()
    fake_creds.expired = False
    fake_creds.refresh_token = "r"
    google_creds.return_value = fake_creds
    with patch("keyring.get_password", return_value=json.dumps(tok)):
        result = auth.get_credentials()
    assert result is fake_creds
    # Token fields were threaded into the Credentials constructor.
    _, kwargs = google_creds.call_args
    assert kwargs["token"] == "t"
    assert kwargs["refresh_token"] == "r"
    assert kwargs["client_id"] == "cid"
    # Not expired → no refresh attempt.
    fake_creds.refresh.assert_not_called()


def test_get_credentials_refreshes_expired_token_and_persists(data_dir, google_creds):
    tok = {"token": "old", "refresh_token": "r", "client_id": "cid"}
    fake_creds = MagicMock()
    fake_creds.expired = True
    fake_creds.refresh_token = "r"
    fake_creds.token = "new"
    fake_creds.token_uri = "uri"
    fake_creds.client_id = "cid"
    fake_creds.client_secret = "sec"
    fake_creds.scopes = ["s"]
    google_creds.return_value = fake_creds
    with (
        patch("keyring.get_password", return_value=json.dumps(tok)),
        patch("keyring.set_password") as set_pw,
    ):
        result = auth.get_credentials()
    assert result is fake_creds
    fake_creds.refresh.assert_called_once()
    # Refreshed creds were saved back.
    set_pw.assert_called_once()
    saved = json.loads(set_pw.call_args[0][2])
    assert saved["token"] == "new"


def test_get_credentials_returns_none_on_invalid_grant(data_dir, google_creds):
    tok = {"token": "old", "refresh_token": "r"}
    fake_creds = MagicMock()
    fake_creds.expired = True
    fake_creds.refresh_token = "r"
    fake_creds.refresh.side_effect = RuntimeError("invalid_grant: token revoked")
    google_creds.return_value = fake_creds
    with patch("keyring.get_password", return_value=json.dumps(tok)):
        result = auth.get_credentials()
    assert result is None


def test_get_credentials_returns_stale_creds_on_other_refresh_error(data_dir, google_creds):
    """A non-fatal refresh error keeps the (stale) creds rather than dropping them."""
    tok = {"token": "old", "refresh_token": "r"}
    fake_creds = MagicMock()
    fake_creds.expired = True
    fake_creds.refresh_token = "r"
    fake_creds.refresh.side_effect = RuntimeError("transient network blip")
    google_creds.return_value = fake_creds
    with patch("keyring.get_password", return_value=json.dumps(tok)):
        result = auth.get_credentials()
    assert result is fake_creds


# ─── Authorization URL ──────────────────────────────────────────────────────


def test_build_authorization_url_returns_url_and_state(data_dir, google_flow):
    auth.save_client_secrets({"installed": {"client_id": "cid", "client_secret": "sec"}})

    fake_flow = MagicMock()
    fake_flow.authorization_url.return_value = ("https://accounts.google/auth?x=1", "STATE123")
    google_flow.from_client_config.return_value = fake_flow

    url, state = auth.build_authorization_url(state="STATE123")

    assert url == "https://accounts.google/auth?x=1"
    assert state == "STATE123"
    # Flow built from the unwrapped secrets under an "installed" key.
    cfg, kwargs = google_flow.from_client_config.call_args
    assert cfg[0] == {"installed": {"client_id": "cid", "client_secret": "sec"}}
    assert kwargs["scopes"] == auth.SCOPES
    # State threaded through to Google's authorization_url builder.
    assert fake_flow.authorization_url.call_args.kwargs["state"] == "STATE123"


def test_build_authorization_url_raises_without_secrets(data_dir, google_flow):
    with pytest.raises(FileNotFoundError):
        auth.build_authorization_url()


# ─── exchange_code ──────────────────────────────────────────────────────────


def test_exchange_code_success_persists_token(data_dir, google_flow):
    # Pre-store the client in the keyring so the single set_password below is
    # unambiguously the token write (not a migration side effect).
    auth.save_client_secrets({"installed": {"client_id": "cid", "client_secret": "sec"}})

    fake_creds = MagicMock()
    fake_creds.token = "newtok"
    fake_creds.refresh_token = "newrefresh"
    fake_creds.token_uri = "uri"
    fake_creds.client_id = "cid"
    fake_creds.client_secret = "sec"
    fake_creds.scopes = ["s"]
    fake_flow = MagicMock()
    fake_flow.credentials = fake_creds
    google_flow.from_client_config.return_value = fake_flow

    with patch("keyring.set_password") as set_pw:
        data = auth.exchange_code("authcode-xyz")

    fake_flow.fetch_token.assert_called_once_with(code="authcode-xyz")
    assert data["token"] == "newtok"
    assert data["refresh_token"] == "newrefresh"
    # Token was persisted.
    set_pw.assert_called_once()
    assert json.loads(set_pw.call_args[0][2])["token"] == "newtok"


def test_exchange_code_propagates_fetch_failure(data_dir, google_flow):
    auth.save_client_secrets({"installed": {"client_id": "cid", "client_secret": "sec"}})

    fake_flow = MagicMock()
    fake_flow.fetch_token.side_effect = ValueError("bad code")
    google_flow.from_client_config.return_value = fake_flow

    with patch("keyring.set_password"):
        with pytest.raises(ValueError):
            auth.exchange_code("bad")


def test_exchange_code_raises_without_secrets(data_dir, google_flow):
    with pytest.raises(FileNotFoundError):
        auth.exchange_code("code")
