"""Google Calendar OAuth + calendar picker endpoints.

The OAuth state set is kept in process memory because it only needs to
survive the few seconds between ``/auth/url`` (issues the state) and
``/auth/callback`` (consumes it). A server restart in that window is
equivalent to the user dropping the flow and trying again.
"""

from __future__ import annotations

import asyncio
import html
import json
import os
from os import replace as os_replace
from pathlib import Path

import structlog
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from estormi_server.api._oauth_base import render_oauth_page
from estormi_server.api._oauth_state import OAuthStateCache
from estormi_server.api._validation import validate_group_type
from estormi_server.server.limiter import limiter
from estormi_server.services import calendar_oauth as svc
from estormi_server.storage.tools import get_write_lock, sqlite_conn

log = structlog.get_logger()

router = APIRouter()

# OAuth state cache — bounded, TTL-pruned. Shared implementation with the WHOOP
# flow; see ``api._oauth_state``.
_OAUTH_STATES = OAuthStateCache()


@router.get("/api/calendar/auth/url")
@limiter.limit("10/minute")
async def gcal_auth_url(request: Request):
    try:
        from estormi_ingestion.google_calendar import auth as gcal_auth  # noqa: PLC0415

        url, state = gcal_auth.build_authorization_url()
        _OAUTH_STATES.add(state)
        return {"url": url, "state": state}
    except FileNotFoundError as e:
        # Path of the missing client-secrets file is server-side info the
        # SPA renders verbatim — keep the message generic so it doesn't
        # leak the data dir layout to a remote caller.
        log.warning("gcal.oauth.url.missing_secrets", error=str(e))
        raise HTTPException(
            status_code=400,
            detail="Google client secrets are not configured",
        )
    except Exception:  # noqa: BLE001
        log.exception("gcal.oauth.url.error")
        raise HTTPException(status_code=500, detail="oauth url error")


class GCalCallbackBody(BaseModel):
    code: str = Field(..., min_length=1, max_length=2048)
    state: str = Field(default="", max_length=256)


@router.post("/api/calendar/auth/callback")
@limiter.limit("10/minute")
async def gcal_auth_callback(request: Request, body: GCalCallbackBody):
    try:
        from estormi_ingestion.google_calendar import auth as gcal_auth  # noqa: PLC0415

        # State is required: the OAuth flow always issues one on /auth/url
        # and Google echoes it back. A missing or unknown state means
        # either CSRF or a flow that crossed a server restart — neither
        # is a case we should silently accept.
        # NB: ``_OAUTH_STATES`` being empty after a process restart is an
        # acceptable failure — the user just retries from the SPA.
        if not body.state:
            raise HTTPException(status_code=400, detail="invalid state")
        if body.state not in _OAUTH_STATES:
            raise HTTPException(status_code=400, detail="invalid state")
        _OAUTH_STATES.consume(body.state)
        # Blocking HTTPS round-trip to Google's token endpoint — keep it
        # off the event loop so concurrent requests are not stalled while
        # the exchange completes.
        await asyncio.to_thread(gcal_auth.exchange_code, body.code)
        return {"ok": True}
    except HTTPException:
        raise
    except Exception:  # noqa: BLE001
        log.exception("gcal.oauth.callback.error")
        raise HTTPException(status_code=500, detail="oauth callback error")


def _oauth_html(body: str, status_code: int = 200):
    return render_oauth_page("Google Calendar", body, status_code=status_code)


@router.get("/api/calendar/auth/callback")
@limiter.limit("10/minute")
async def gcal_auth_callback_get(
    request: Request,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
):
    """Browser-redirect handler for the Google OAuth flow.

    Google redirects here with ``?code=…`` on success or ``?error=…``
    when the user is blocked (most commonly ``access_denied`` when the
    OAuth consent screen is in "Testing" mode and the user hasn't been
    added as a test user). We render a single ergonomic landing page in
    both branches with a remediation recipe — closing the tab and
    staring at the Google error screen is a dead end without it.
    """
    if error or not code:
        # Map Google's standard OAuth error codes to an actionable line.
        # ``access_denied`` covers BOTH "the user clicked Cancel" and
        # "Google refused because consent screen is in Testing with no
        # test user matching the signed-in account"; we lean into the
        # second because that's the common case for new installs.
        err = (error or "no_code").strip()
        err_safe = html.escape(err)
        if err == "access_denied":
            return _oauth_html(
                "<h2 class='err'>Google blocked the consent flow ("
                "<code>access_denied</code>)</h2>"
                "<p>This almost always means your OAuth consent screen is in "
                "<strong>Testing</strong> mode and the Google account you just "
                "signed in with isn't on the test-users list. Two quick fixes "
                "— pick one:</p>"
                "<ol>"
                "<li><strong>Add yourself as a test user</strong> (recommended). "
                "Open <a href='https://console.cloud.google.com/apis/credentials/consent' "
                "target='_blank' rel='noopener noreferrer'>APIs &amp; Services → "
                "OAuth consent screen</a>, scroll to <em>Test users</em>, click "
                "<strong>+ Add users</strong>, paste the same Gmail address you "
                "just tried to sign in with, save. Then come back to Estormi and "
                "click <strong>Connect with Google</strong> again.</li>"
                "<li><strong>Or publish the app</strong>. Same page, click "
                "<strong>Publish app</strong>. For a Desktop client without "
                "sensitive scopes (Estormi only reads calendar events), Google "
                "skips verification and you can connect immediately.</li>"
                "</ol>"
                "<div class='note'>You can close this tab once you've done either "
                "of the above.</div>",
                status_code=403,
            )
        if err == "no_code":
            return _oauth_html(
                "<h2 class='err'>Estormi didn't receive an authorization code.</h2>"
                "<p>Looks like the OAuth flow was cancelled or the redirect URL "
                "lost the <code>code</code> parameter on its way back. Retry "
                "from inside Estormi — click <strong>Connect with Google</strong> "
                "again.</p>",
                status_code=400,
            )
        return _oauth_html(
            "<h2 class='err'>Google returned an OAuth error.</h2>"
            f"<p>Google said: <code>{err_safe}</code>. Retry the connection from "
            "inside Estormi; if it persists, check that your OAuth client in "
            "<a href='https://console.cloud.google.com/apis/credentials' "
            "target='_blank' rel='noopener noreferrer'>Google Cloud Console</a> "
            "is type <em>Desktop app</em> and the Calendar API is enabled.</p>",
            status_code=400,
        )
    try:
        from estormi_ingestion.google_calendar import auth as gcal_auth  # noqa: PLC0415

        # State must be present AND known. The in-memory map being empty
        # after a server restart counts as failure here — the user
        # simply retries from the SPA, which mints a fresh state.
        if not state or state not in _OAUTH_STATES:
            return _oauth_html(
                "<h2 class='err'>OAuth state mismatch.</h2>"
                "<p>Please retry the connection from inside Estormi.</p>",
                status_code=400,
            )
        _OAUTH_STATES.consume(state)
        # Blocking HTTPS round-trip to Google's token endpoint — off the
        # event loop so other in-flight requests aren't stalled.
        await asyncio.to_thread(gcal_auth.exchange_code, code)
    except Exception:  # noqa: BLE001
        # Do not echo the raw exception (it can contain paths, tokens, or
        # internal state). Show a stable user-facing code; the real error
        # is on the server's structured log.
        return _oauth_html(
            "<h2 class='err'>Could not exchange the authorization code.</h2>"
            "<p>Error code: <code>oauth_exchange_failed</code>. Retry the "
            "connection from inside Estormi.</p>",
            status_code=500,
        )
    return _oauth_html(
        "<h2 class='ok'>✓ Estormi is now connected to Google Calendar.</h2>"
        "<p>You can close this tab and return to the Estormi window — your "
        "calendars are ready to pick.</p>"
    )


class GCalSecretsBody(BaseModel):
    """OAuth client JSON uploaded by the user via the GoogleCalendarPanel.

    The Estormi UI lets the user drop the JSON they downloaded from
    Google Cloud Console straight into the connection panel; the server
    validates the shape (it must carry either an ``installed`` or
    ``web`` key with ``client_id`` / ``client_secret``) and persists it
    to ``$DATA_DIR/google_client_secrets.json`` — the same path the
    rest of the codebase already reads from.
    """

    # Google client-secret JSON is < 1 KB in practice; the 16 KB cap
    # leaves headroom for hand-edited files without letting a hostile
    # caller force an unbounded ``json.loads`` on the event loop.
    content: str = Field(..., max_length=16_384)


@router.post("/api/calendar/secrets/upload")
@limiter.limit("10/minute")
async def gcal_secrets_upload(request: Request, body: GCalSecretsBody):
    from estormi_ingestion.google_calendar.auth import _client_secrets_path  # noqa: PLC0415

    # 1) Parse the upload as JSON. We accept either a raw string from
    #    `FileReader.readAsText()` or a stringified object.
    try:
        data = json.loads(body.content)
    except json.JSONDecodeError as e:
        # Don't echo the raw parser text back to the client — match the MCP
        # handler's redaction invariant (test_security_boundary). Log the detail.
        log.info("calendar_oauth.client_upload_invalid_json", error=str(e))
        raise HTTPException(status_code=400, detail="invalid OAuth client JSON")

    if not isinstance(data, dict):
        raise HTTPException(status_code=400, detail="expected a JSON object at the top level")

    # 2) Validate it looks like a Google OAuth client file. Google emits
    #    two shapes: "Desktop app" → {"installed": {...}}, "Web app" →
    #    {"web": {...}}. Estormi uses an installed-app flow (system
    #    browser → loopback redirect) so we surface a clearer error if
    #    the user picked the wrong type.
    inner = data.get("installed") or data.get("web") or {}
    if not isinstance(inner, dict) or "client_id" not in inner or "client_secret" not in inner:
        raise HTTPException(
            status_code=400,
            detail=(
                "doesn't look like a Google OAuth client JSON — expected "
                "an 'installed' or 'web' key with 'client_id' and "
                "'client_secret' inside."
            ),
        )
    if "web" in data and "installed" not in data:
        raise HTTPException(
            status_code=400,
            detail=(
                "this is a 'Web application' OAuth client. Estormi needs "
                "a 'Desktop app' client — create one in Google Cloud "
                "Console and re-upload."
            ),
        )

    # 3) Persist. Atomic write: ``<tmp>`` + ``rename`` so a partial
    #    write can never leave a half-baked secrets file behind. The tmp is
    #    created 0o600 from the start (mirrors the WHOOP token path) so the
    #    OAuth client secret is never world-readable, not even briefly.
    target = Path(_client_secrets_path())
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(".json.tmp")
    payload = json.dumps(data, indent=2)

    def _write_secret() -> None:
        # O_TRUNC tolerates a leftover tmp from a crashed prior run; the
        # explicit fchmod guarantees 0o600 even when O_CREAT's mode is ignored
        # (the file already existed).
        fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        # fdopen takes ownership of the fd; the with-block closes it on exit
        # (and on error) — no separate os.close, which would double-close.
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            # Belt-and-suspenders: a pre-existing tmp keeps its old mode (O_CREAT's
            # mode arg is ignored), so re-assert 0o600. Best-effort per the
            # data-at-rest convention.
            try:
                os.fchmod(f.fileno(), 0o600)
            except OSError:
                pass
            f.write(payload)
        os_replace(str(tmp), str(target))

    try:
        await asyncio.to_thread(_write_secret)
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass
    return {
        "ok": True,
        "path": str(target),
        "client_type": "installed" if "installed" in data else "web",
    }


@router.post("/api/calendar/auth/open")
@limiter.limit("10/minute")
async def gcal_auth_open(request: Request):
    """Build the OAuth URL and open it in the system default browser.

    The SPA uses this instead of ``window.open`` so the URL fires
    through ``open(1)`` on macOS (Safari / Chrome / Firefox), bypassing
    any Tauri webview interception that could otherwise trap the
    OAuth-consent page inside the app.
    """
    import subprocess  # noqa: PLC0415

    try:
        from estormi_ingestion.google_calendar import auth as gcal_auth  # noqa: PLC0415

        url, state = gcal_auth.build_authorization_url()
        _OAUTH_STATES.add(state)
        try:
            await asyncio.to_thread(subprocess.Popen, ["open", url])
        except Exception as e:  # noqa: BLE001
            log.warning("gcal.oauth.open.popen_failed", error=str(e))
            return {"opened": False, "url": url, "state": state, "error": "could not open browser"}
        return {"opened": True, "url": url, "state": state}
    except FileNotFoundError as e:
        # Generic message — the missing-file path is server-side info; don't
        # leak the data-dir layout to a caller (mirrors gcal_auth_url above).
        log.warning("gcal.oauth.open.missing_secrets", error=str(e))
        raise HTTPException(status_code=400, detail="Google client secrets are not configured")
    except Exception:  # noqa: BLE001
        log.exception("gcal.oauth.open.error")
        raise HTTPException(status_code=500, detail="oauth open error")


@router.delete("/api/calendar/auth")
@limiter.limit("10/minute")
async def gcal_auth_delete(request: Request):
    from estormi_ingestion.google_calendar import auth as gcal_auth  # noqa: PLC0415

    gcal_auth.delete_token()
    db = sqlite_conn()
    # Serialise on the shared write lock so a concurrent leaf writer's commit
    # can't tear this DELETE→commit span. Leaf — not re-entrant. See
    # ``tools._write_lock``.
    async with get_write_lock():
        await db.execute(
            "DELETE FROM settings WHERE key = ?",
            ("google_calendar_sync_token",),
        )
        await db.commit()
    return {"ok": True}


@router.post("/api/google-calendar/sync-token/reset")
@limiter.limit("10/minute")
async def gcal_reset_sync_token(request: Request):
    """Drop the stored per-calendar ``nextSyncToken`` map.

    Google Calendar has no watermark — it syncs incrementally off opaque
    sync tokens (see ``estormi_ingestion/google_calendar/sync.py``). Deleting the
    ``google_calendar_sync_token`` row forces every selected calendar back
    to a first-run full pull on the next gcal stage. OAuth is left intact;
    re-ingest is idempotent (``/ingest_chunk`` dedupes on content_hash) so
    this only widens the window, it doesn't duplicate events.
    """
    db = sqlite_conn()
    async with get_write_lock():
        await db.execute(
            "DELETE FROM settings WHERE key = ?",
            ("google_calendar_sync_token",),
        )
        await db.commit()
    return {"ok": True}


@router.get("/api/google-calendar/calendars")
@limiter.limit("10/minute")
async def gcal_list_calendars(request: Request):
    from estormi_ingestion.google_calendar import auth as gcal_auth  # noqa: PLC0415
    from estormi_ingestion.google_calendar import sync as gcal_sync  # noqa: PLC0415

    creds = gcal_auth.get_credentials()
    if creds is None:
        raise HTTPException(status_code=401, detail="not authenticated")

    db = sqlite_conn()
    selected_ids = await svc.selected_ids(db)
    group_types = await svc.group_types(db)

    try:
        # Both _build_service and _list_user_calendars call into googleapiclient's
        # blocking HTTP client (sync round-trips to Google's Calendar API). Push
        # them off the event loop so a slow Google response can't stall every
        # other in-flight request — same treatment as exchange_code above.
        service = await asyncio.to_thread(gcal_sync._build_service, creds)
        items = await asyncio.to_thread(gcal_sync._list_user_calendars, service)
    except Exception as e:  # noqa: BLE001
        # When the access token has expired locally but googleapiclient's
        # lazy refresh discovers the refresh token itself is dead, the
        # exception surfaces here — not in get_credentials(). Detect it
        # by class name + payload (the import-light check used elsewhere)
        # and return 401 so the SPA flips the panel into its
        # "needs re-auth" branch instead of "Error talking to Google
        # Calendar". Also wipe the dead token so we stop hammering
        # Google with a credential we know is revoked.
        if type(e).__name__ == "RefreshError" or "invalid_grant" in str(e):
            try:
                gcal_auth.delete_token()
            except Exception:  # noqa: BLE001
                pass
            raise HTTPException(status_code=401, detail="refresh token revoked")
        # Generic detail (the upstream text can carry tokens/URLs); full
        # exception goes to the server log, mirroring the MCP redaction.
        log.warning("calendar_oauth.google_api_error", error=str(e))
        raise HTTPException(status_code=502, detail="Google Calendar API request failed")

    return [
        {
            "id": c["id"],
            "name": c.get("summary", c["id"]),
            "color": c.get("backgroundColor"),
            "selected": (c["id"] in selected_ids) if selected_ids else True,
            "group_type": group_types.get(c["id"], "unknown"),
        }
        for c in items
    ]


class GCalSelectBody(BaseModel):
    """Partial-update body for one Google calendar row.

    Both fields optional: the SPA sends ``{selected: true}`` from the
    toggle and ``{group_type: "work"}`` from the chip selector. The
    handler accepts either or both in one round-trip.
    """

    selected: bool | None = None
    group_type: str | None = None


@router.patch("/api/google-calendar/calendars/{calendar_id:path}")
@limiter.limit("10/minute")
async def gcal_set_selected(request: Request, calendar_id: str, body: GCalSelectBody):
    if body.group_type is not None:
        validate_group_type(body.group_type, svc.GCAL_GROUP_TYPES)
    db = sqlite_conn()
    retagged = await svc.apply_selection_update(db, calendar_id, body.selected, body.group_type)
    return {
        "ok": True,
        "calendar_id": calendar_id,
        "selected": body.selected,
        "group_type": body.group_type,
        "retagged": retagged,
    }
