"""WHOOP OAuth endpoints — credential upload, consent flow, status, disconnect.

Mirrors ``api/calendar_oauth.py`` but for the WHOOP Cloud API, which is a
plain OAuth2 authorization-code flow (no vendor SDK). The OAuth state set is
kept in process memory because it only needs to survive the few seconds
between ``/auth/url`` (issues the state) and ``/auth/callback`` (consumes it);
a server restart in that window is equivalent to the user dropping the flow
and retrying.
"""

from __future__ import annotations

import asyncio
import secrets

import structlog
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from estormi_server.api._oauth_base import render_oauth_page
from estormi_server.api._oauth_state import OAuthStateCache
from estormi_server.server.limiter import limiter

log = structlog.get_logger()

router = APIRouter()

# OAuth state cache — bounded, TTL-pruned. Shared implementation with the gcal
# flow; see ``api._oauth_state``.
_OAUTH_STATES = OAuthStateCache()


class WhoopCredentialsBody(BaseModel):
    """The client_id / client_secret pasted from developer.whoop.com.

    WHOOP client ids/secrets are short opaque strings; the 512-char cap is
    generous headroom without letting a hostile caller send a megabyte.
    """

    client_id: str = Field(..., min_length=1, max_length=512)
    client_secret: str = Field(..., min_length=1, max_length=512)


@router.post("/api/whoop/credentials/upload")
@limiter.limit("10/minute")
async def whoop_credentials_upload(request: Request, body: WhoopCredentialsBody):
    from estormi_ingestion.whoop import auth as whoop_auth  # noqa: PLC0415

    await asyncio.to_thread(
        whoop_auth.save_client, body.client_id.strip(), body.client_secret.strip()
    )
    return {"ok": True}


@router.get("/api/whoop/status")
@limiter.limit("30/minute")
async def whoop_status(request: Request):
    """Drive the Settings panel's three states: setup / disconnected / connected.

    ``client`` false → no credentials yet (setup). ``connected`` true → a token
    is stored and still refreshable. A stored-but-dead refresh token reads as
    ``connected: false`` so the panel offers a reconnect.
    """
    from estormi_ingestion.whoop import auth as whoop_auth  # noqa: PLC0415

    client_present = await asyncio.to_thread(whoop_auth.client_present)
    if not client_present:
        return {"client": False, "connected": False, "redirect_uri": whoop_auth.redirect_uri()}
    # A stored token whose refresh still works counts as connected; the
    # refresh is a blocking HTTPS round-trip, so push it off the event loop.
    token = await asyncio.to_thread(whoop_auth.load_token)
    connected = False
    if token is not None:
        access = await asyncio.to_thread(whoop_auth.get_access_token)
        connected = access is not None
    return {"client": True, "connected": connected, "redirect_uri": whoop_auth.redirect_uri()}


@router.post("/api/whoop/auth/open")
@limiter.limit("10/minute")
async def whoop_auth_open(request: Request):
    """Build the consent URL and open it in the system default browser.

    Uses ``open(1)`` so the URL fires through Safari/Chrome rather than being
    trapped inside the Tauri webview. Same rationale as the gcal flow.
    """
    import subprocess  # noqa: PLC0415

    from estormi_ingestion.whoop import auth as whoop_auth  # noqa: PLC0415

    try:
        state = secrets.token_urlsafe(24)
        url = await asyncio.to_thread(whoop_auth.build_authorization_url, state)
        _OAUTH_STATES.add(state)
        try:
            await asyncio.to_thread(subprocess.Popen, ["open", url])
        except Exception as e:  # noqa: BLE001
            log.warning("whoop.oauth.open.popen_failed", error=str(e))
            return {"opened": False, "url": url, "state": state, "error": "could not open browser"}
        return {"opened": True, "url": url, "state": state}
    except FileNotFoundError:
        # No client credentials saved yet — distinct from a server fault.
        raise HTTPException(status_code=400, detail="WHOOP client credentials are not configured")
    except Exception:  # noqa: BLE001
        log.exception("whoop.oauth.open.error")
        raise HTTPException(status_code=500, detail="oauth open error")


def _oauth_html(body: str, status_code: int = 200):
    return render_oauth_page("WHOOP", body, status_code=status_code)


@router.get("/api/whoop/auth/callback")
@limiter.limit("10/minute")
async def whoop_auth_callback(
    request: Request,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
):
    """Browser-redirect handler for the WHOOP OAuth flow."""
    if error or not code:
        return _oauth_html(
            "<h2 class='err'>WHOOP didn't return an authorization code.</h2>"
            "<p>The consent flow was cancelled, or the redirect lost the "
            "<code>code</code> parameter. Retry from inside Estormi — click "
            "<strong>Connect with WHOOP</strong> again. If it persists, check "
            "that your app's redirect URI on developer.whoop.com matches "
            "exactly.</p>",
            status_code=400,
        )
    # State must be present AND known. An empty map after a server restart
    # counts as failure — the user retries from the SPA, which mints a fresh
    # state.
    if not state or state not in _OAUTH_STATES:
        return _oauth_html(
            "<h2 class='err'>OAuth state mismatch.</h2>"
            "<p>Please retry the connection from inside Estormi.</p>",
            status_code=400,
        )
    _OAUTH_STATES.consume(state)
    try:
        from estormi_ingestion.whoop import auth as whoop_auth  # noqa: PLC0415

        # Blocking HTTPS round-trip to WHOOP's token endpoint — off the event
        # loop so other in-flight requests aren't stalled.
        await asyncio.to_thread(whoop_auth.exchange_code, code)
    except Exception:  # noqa: BLE001
        # Never echo the raw exception (it can carry the client secret or
        # token). Stable user-facing code; real error is in the server log.
        log.exception("whoop.oauth.callback.error")
        return _oauth_html(
            "<h2 class='err'>Could not exchange the authorization code.</h2>"
            "<p>Error code: <code>oauth_exchange_failed</code>. Retry the "
            "connection from inside Estormi.</p>",
            status_code=500,
        )
    return _oauth_html(
        "<h2 class='ok'>✓ Estormi is now connected to WHOOP.</h2>"
        "<p>You can close this tab and return to the Estormi window — your "
        "recovery, sleep and strain will sync on the next run.</p>"
    )


@router.delete("/api/whoop/auth")
@limiter.limit("10/minute")
async def whoop_auth_delete(request: Request):
    from estormi_ingestion.whoop import auth as whoop_auth  # noqa: PLC0415

    await asyncio.to_thread(whoop_auth.delete_token)
    return {"ok": True}
