"""
WHOOP OAuth2 helpers (plain authorization-code flow over the WHOOP Cloud API).

Token is stored in the system keyring under service ``estormi.whoop`` key
``oauth_token``. If keyring is unavailable (headless / locked) we fall back to
a chmod-600 file at ``DATA_DIR/.whoop_token``.

App credentials (client_id / client_secret) live in the **keyring only**, under
the same service with key ``client`` — written by the Settings UI when the user
pastes the two values from their app on developer.whoop.com. They are never
mirrored to a cleartext file; a one-time migration imports any legacy
``DATA_DIR/whoop_client.json`` into the keyring and deletes it.

WHOOP, unlike Google, has no SDK we depend on — the flow is hand-rolled with
``httpx`` against the documented endpoints:

* authorize URL : https://api.prod.whoop.com/oauth/oauth2/auth
* token URL     : https://api.prod.whoop.com/oauth/oauth2/token

WHOOP **rotates the refresh token on every refresh** (the old one is
invalidated the moment a new access token is minted), so ``get_access_token``
must persist the new token dict after each refresh or the next run is locked
out. This is the single sharpest edge of the integration.
"""

from __future__ import annotations

import os
import threading
import time
from typing import Any

import httpx
import structlog

from estormi_ingestion.shared import token_store
from estormi_ingestion.shared.paths import estormi_data_dir

log = structlog.get_logger()

SERVICE_NAME = "estormi.whoop"
TOKEN_KEY = "oauth_token"
CLIENT_KEY = "client"  # pragma: allowlist secret  (keyring key name, not a secret)

AUTH_URL = "https://api.prod.whoop.com/oauth/oauth2/auth"
TOKEN_URL = "https://api.prod.whoop.com/oauth/oauth2/token"

# ``offline`` is what makes WHOOP issue a refresh token — without it every
# access token dies in an hour and the nightly pipeline can never run unattended.
SCOPES = [
    "read:recovery",
    "read:sleep",
    "read:cycles",
    "read:workout",
    "read:profile",
    "offline",
]

# Refresh a little before the real expiry so a long-running sync started near
# the boundary doesn't have the token die mid-pagination.
_EXPIRY_SKEW_SECONDS = 120

# Serializes the load→check-expiry→refresh→persist critical section of
# ``get_access_token``. WHOOP rotates (and invalidates) the refresh token on
# every refresh, so two concurrent callers must NOT both POST the same stored
# refresh token: the second presents a token WHOOP already consumed → 400
# invalid_grant → spurious "disconnected" + last-writer clobbers the fresh
# token. ``get_access_token`` is sync and called via ``asyncio.to_thread`` on
# real worker threads (e.g. the wake-poller and GET /api/whoop/status), so a
# ``threading.Lock`` — not an ``asyncio.Lock`` — is the right primitive.
_refresh_lock = threading.Lock()


def _token_file() -> str:
    return str(estormi_data_dir() / ".whoop_token")


def _legacy_client_file() -> str:
    """Pre-keyring cleartext location, kept only as a one-time migration source."""
    return str(estormi_data_dir() / "whoop_client.json")


# ─── Token storage ─────────────────────────────────────────────────────────


def save_token(data: dict[str, Any]) -> None:
    """Persist OAuth token (keyring first, chmod-600 file fallback)."""
    token_store.save_token(SERVICE_NAME, TOKEN_KEY, data, token_file=_token_file())


def load_token() -> dict[str, Any] | None:
    """Read OAuth token (keyring first, file fallback)."""
    return token_store.load_token(SERVICE_NAME, TOKEN_KEY, token_file=_token_file())


def delete_token() -> None:
    """Delete the stored token (WHOOP has no documented revoke endpoint)."""
    token_store.delete_token(SERVICE_NAME, TOKEN_KEY, token_file=_token_file())


# ─── Client credentials ────────────────────────────────────────────────────


def save_client(client_id: str, client_secret: str) -> None:
    """Persist the app credentials the user pasted from developer.whoop.com.

    Keyring only — the client secret never lands in a cleartext file.
    """
    token_store.save_secret(
        SERVICE_NAME, CLIENT_KEY, {"client_id": client_id, "client_secret": client_secret}
    )


def load_client() -> dict[str, str]:
    """Read the app credentials from the keyring, migrating a legacy file once.

    Raises ``FileNotFoundError`` when nothing is stored yet and ``ValueError``
    when the stored value is malformed — the two cases ``client_present``
    treats as "not configured".
    """
    data = token_store.load_secret(SERVICE_NAME, CLIENT_KEY)
    if data is None:
        data = token_store.migrate_file_to_keyring(
            SERVICE_NAME, CLIENT_KEY, legacy_file=_legacy_client_file()
        )
    if data is None:
        raise FileNotFoundError(
            "WHOOP client credentials are not set. "
            "Add your WHOOP app's client_id / client_secret in Settings."
        )
    if not data.get("client_id") or not data.get("client_secret"):
        raise ValueError("stored WHOOP client credentials are missing client_id or client_secret")
    return {"client_id": str(data["client_id"]), "client_secret": str(data["client_secret"])}


def client_present() -> bool:
    try:
        load_client()
        return True
    except (FileNotFoundError, ValueError):
        return False


def redirect_uri() -> str:
    """Loopback redirect the WHOOP app must whitelist.

    Must match — to the character — a redirect URI registered on the WHOOP
    developer dashboard. The server listens on ``MCP_SERVER_PORT`` (8000 by
    default); the browser-redirect handler lives at
    ``/api/whoop/auth/callback``.
    """
    port = os.getenv("MCP_SERVER_PORT", "8000")
    return f"http://localhost:{port}/api/whoop/auth/callback"


# ─── OAuth flow ────────────────────────────────────────────────────────────


def build_authorization_url(state: str) -> str:
    """Return the WHOOP consent URL for the given (caller-minted) state."""
    from urllib.parse import urlencode  # noqa: PLC0415

    client = load_client()
    params = {
        "response_type": "code",
        "client_id": client["client_id"],
        "redirect_uri": redirect_uri(),
        "scope": " ".join(SCOPES),
        "state": state,
    }
    return f"{AUTH_URL}?{urlencode(params)}"


def _store_token_response(payload: dict[str, Any]) -> dict[str, Any]:
    """Stamp an absolute expiry onto a token response and persist it."""
    expires_in = int(payload.get("expires_in", 3600))
    data = dict(payload)
    data["expires_at"] = time.time() + expires_in - _EXPIRY_SKEW_SECONDS
    save_token(data)
    return data


def exchange_code(code: str) -> dict[str, Any]:
    """Exchange an authorization code for a token and persist it."""
    client = load_client()
    resp = httpx.post(
        TOKEN_URL,
        data={
            "grant_type": "authorization_code",
            "code": code,
            "client_id": client["client_id"],
            "client_secret": client["client_secret"],
            "redirect_uri": redirect_uri(),
        },
        timeout=30,
    )
    resp.raise_for_status()
    return _store_token_response(resp.json())


def _refresh(token: dict[str, Any]) -> dict[str, Any] | None:
    """Mint a fresh access token from the stored refresh token.

    Persists the ROTATED refresh token WHOOP returns. Returns ``None`` when
    the refresh token is dead (revoked / expired) so the caller can emit a
    clean "needs re-auth" branch instead of a traceback.
    """
    refresh_token = token.get("refresh_token")
    if not refresh_token:
        return None
    client = load_client()
    resp = httpx.post(
        TOKEN_URL,
        data={
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": client["client_id"],
            "client_secret": client["client_secret"],
            # WHOOP requires ``offline`` on refresh too, otherwise the new
            # token comes back WITHOUT a refresh token and the next run is
            # locked out.
            "scope": "offline",
        },
        timeout=30,
    )
    if resp.status_code in (400, 401):
        # A definitive invalid_grant: the stored refresh token is dead. Delete
        # it so later polls/syncs don't re-POST a known-dead token to WHOOP
        # forever (futile, and it trips WHOOP's reuse-detection noise). A
        # transient network error raises an httpx exception instead of a
        # 400/401, so this won't wipe a valid token on a blip; a later
        # reconnect re-creates the token.
        log.warning("whoop refresh token revoked or expired: %s", resp.text[:200])
        delete_token()
        return None
    resp.raise_for_status()
    return _store_token_response(resp.json())


def get_access_token() -> str | None:
    """Return a valid access token, refreshing (and rotating) as needed.

    ``None`` means no usable credentials — either nothing stored yet or the
    refresh token is dead and the user must reconnect from Settings.

    The load→check-expiry→refresh section runs under ``_refresh_lock`` so
    concurrent callers can't double-spend WHOOP's single-use rotating refresh
    token. A caller that blocks behind another thread's refresh RE-READS the
    token after acquiring the lock, picking up the freshly-rotated one instead
    of re-spending the consumed one.
    """
    with _refresh_lock:
        token = load_token()
        if not token:
            return None
        if time.time() < float(token.get("expires_at", 0)):
            return token.get("access_token")
        refreshed = _refresh(token)
        if refreshed is None:
            return None
        return refreshed.get("access_token")
