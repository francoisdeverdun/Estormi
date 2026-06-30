"""
Google Calendar OAuth2 helpers.

Token is stored in the system keyring under service ``estormi.google_calendar``
key ``oauth_token``. If keyring is unavailable (headless / locked) we fall
back to a chmod-600 file at ``DATA_DIR/.gcal_token``.

App credentials (client_id / client_secret) — the JSON you download from
Google Cloud Console for an "OAuth client ID" of type Desktop — live in the
**keyring only**, under the same service with key ``client_secrets`` (the
``installed``/``web`` envelope unwrapped). They are never mirrored to a
cleartext file; a one-time migration imports any legacy
``DATA_DIR/google_client_secrets.json`` into the keyring and deletes it.
"""

from __future__ import annotations

import os
from typing import Any

import httpx
import structlog

from estormi_ingestion.shared import token_store
from estormi_ingestion.shared.paths import estormi_data_dir

log = structlog.get_logger()

SERVICE_NAME = "estormi.google_calendar"
TOKEN_KEY = "oauth_token"
CLIENT_KEY = "client_secrets"  # pragma: allowlist secret  (keyring key name, not a secret)
SCOPES = ["https://www.googleapis.com/auth/calendar.readonly"]


def _token_file() -> str:
    return str(estormi_data_dir() / ".gcal_token")


def _legacy_client_secrets_path() -> str:
    """Pre-keyring cleartext location, kept only as a one-time migration source."""
    return str(estormi_data_dir() / "google_client_secrets.json")


def _unwrap_client_secrets(data: dict[str, Any]) -> dict[str, Any]:
    """Strip Google's ``installed``/``web`` envelope down to the inner client."""
    return data.get("installed") or data.get("web") or data


# ─── Token storage ─────────────────────────────────────────────────────────


def save_token(data: dict[str, Any]) -> None:
    """Persist OAuth token (keyring first, chmod-600 file fallback)."""
    token_store.save_token(SERVICE_NAME, TOKEN_KEY, data, token_file=_token_file())


def load_token() -> dict[str, Any] | None:
    """Read OAuth token (keyring first, file fallback)."""
    return token_store.load_token(SERVICE_NAME, TOKEN_KEY, token_file=_token_file())


def delete_token() -> None:
    """Revoke (best-effort) and delete the stored token."""
    tok = load_token()
    if tok and tok.get("token"):
        try:
            httpx.post(
                "https://oauth2.googleapis.com/revoke",
                params={"token": tok["token"]},
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                timeout=10,
            )
        except Exception as e:  # noqa: BLE001
            log.warning("token revoke failed: %s", e)
    token_store.delete_token(SERVICE_NAME, TOKEN_KEY, token_file=_token_file())


# ─── Credentials / OAuth flow ──────────────────────────────────────────────


def save_client_secrets(secrets: dict[str, Any]) -> None:
    """Persist the OAuth client to the keyring (no cleartext file).

    Accepts either the raw uploaded JSON (with its ``installed``/``web``
    envelope) or an already-unwrapped inner dict; stores the inner dict.
    """
    token_store.save_secret(SERVICE_NAME, CLIENT_KEY, _unwrap_client_secrets(secrets))


def _load_client_secrets() -> dict[str, Any]:
    data = token_store.load_secret(SERVICE_NAME, CLIENT_KEY)
    if data is None:
        data = token_store.migrate_file_to_keyring(
            SERVICE_NAME,
            CLIENT_KEY,
            legacy_file=_legacy_client_secrets_path(),
            transform=_unwrap_client_secrets,
        )
    if data is None:
        raise FileNotFoundError(
            "Google OAuth client is not set. "
            "Upload an OAuth Desktop client (Google Cloud Console) in Settings."
        )
    return data


def get_credentials():
    """Build an auto-refreshing ``google.oauth2.credentials.Credentials``."""
    tok = load_token()
    if not tok:
        return None
    from google.auth.transport.requests import Request as GRequest  # type: ignore
    from google.oauth2.credentials import Credentials  # type: ignore

    creds = Credentials(
        token=tok.get("token"),
        refresh_token=tok.get("refresh_token"),
        token_uri=tok.get("token_uri", "https://oauth2.googleapis.com/token"),
        client_id=tok.get("client_id"),
        client_secret=tok.get("client_secret"),
        scopes=tok.get("scopes", SCOPES),
    )
    if creds.expired and creds.refresh_token:
        try:
            creds.refresh(GRequest())
            save_token(_creds_to_dict(creds))
        except Exception as e:  # noqa: BLE001
            # A RefreshError with `invalid_grant` means the refresh token
            # is dead (user revoked access, password change, or Google's
            # 7-day unverified-app expiry). Returning the stale creds
            # would only trigger the same error again deeper in the call
            # stack; returning ``None`` lets the caller emit a clean
            # "no credentials" branch instead of a traceback.
            if type(e).__name__ == "RefreshError" or "invalid_grant" in str(e):
                log.warning("google refresh token revoked or expired: %s", e)
                return None
            log.warning("credential refresh failed: %s", e)
    return creds


def _creds_to_dict(creds) -> dict[str, Any]:
    return {
        "token": creds.token,
        "refresh_token": creds.refresh_token,
        "token_uri": creds.token_uri,
        "client_id": creds.client_id,
        "client_secret": creds.client_secret,
        "scopes": list(creds.scopes or SCOPES),
    }


def _redirect_uri() -> str:
    port = os.getenv("MCP_SERVER_PORT", "8000")
    return f"http://localhost:{port}/api/calendar/auth/callback"


def build_authorization_url(state: str | None = None) -> tuple[str, str]:
    """Return (auth_url, state) for the OAuth consent flow."""
    from google_auth_oauthlib.flow import Flow  # type: ignore

    secrets = _load_client_secrets()
    flow = Flow.from_client_config(
        {"installed": secrets}, scopes=SCOPES, redirect_uri=_redirect_uri()
    )
    auth_url, returned_state = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="true",
        prompt="consent",
        state=state,
    )
    return auth_url, returned_state


def exchange_code(code: str) -> dict[str, Any]:
    """Exchange an authorization code for a token and persist it."""
    from google_auth_oauthlib.flow import Flow  # type: ignore

    secrets = _load_client_secrets()
    flow = Flow.from_client_config(
        {"installed": secrets}, scopes=SCOPES, redirect_uri=_redirect_uri()
    )
    flow.fetch_token(code=code)
    creds = flow.credentials
    data = _creds_to_dict(creds)
    save_token(data)
    return data
