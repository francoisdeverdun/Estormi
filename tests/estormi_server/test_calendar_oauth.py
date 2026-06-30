"""Integration tests for ``api/calendar_oauth.py`` — the Google Calendar
OAuth2 flow and calendar-picker endpoints.

OAuth token handling is security-sensitive, so these tests exercise the
real route behaviour: building the consent URL, exchanging an authorization
code, the in-process state set, error/failure branches, secrets-file
upload validation, and the calendar selection/group-type settings.

Every external boundary is mocked — outbound HTTP to Google's token
endpoint, keyring, the filesystem and ``subprocess``. No test makes a real
network call. The ``estormi_ingestion.google_calendar.auth`` helpers are imported
lazily inside each handler, so they are patched on that module.
"""

from __future__ import annotations

import contextlib
import json
from unittest.mock import MagicMock, patch

import pytest

import estormi_server.api.calendar_oauth as calendar_oauth

pytestmark = pytest.mark.integration


@contextlib.contextmanager
def _patch_gcal(*, auth=None, sync=None):
    """Patch the lazily-imported ``estormi_ingestion.google_calendar`` submodules.

    The route handlers run ``from estormi_ingestion.google_calendar import auth``.
    Once the real ``auth`` submodule has been imported, that form resolves
    the ``auth`` attribute on the already-loaded ``estormi_ingestion.google_calendar``
    package — so patching ``sys.modules`` alone is not enough. We patch the
    package attribute (and ``sys.modules``) so the mock wins regardless of
    import order across the suite.
    """
    import importlib
    import sys

    pkg = importlib.import_module("estormi_ingestion.google_calendar")
    patches: list = []
    if auth is not None:
        patches.append(patch.object(pkg, "auth", auth, create=True))
        patches.append(patch.dict(sys.modules, {"estormi_ingestion.google_calendar.auth": auth}))
    if sync is not None:
        patches.append(patch.object(pkg, "sync", sync, create=True))
        patches.append(patch.dict(sys.modules, {"estormi_ingestion.google_calendar.sync": sync}))
    with contextlib.ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        yield


@pytest.fixture(autouse=True)
def _clear_oauth_states():
    """Each test starts with an empty in-process OAuth state set."""
    calendar_oauth._OAUTH_STATES.clear()
    yield
    calendar_oauth._OAUTH_STATES.clear()


# ── GET /api/calendar/auth/url ─────────────────────────────────────────────


async def test_auth_url_returns_url_and_registers_state(client):
    fake = MagicMock()
    fake.build_authorization_url.return_value = ("https://accounts.google.com/o?x=1", "st-1")
    with _patch_gcal(auth=fake):
        resp = await client.get("/api/calendar/auth/url")
    assert resp.status_code == 200
    body = resp.json()
    assert body == {"url": "https://accounts.google.com/o?x=1", "state": "st-1"}
    # The issued state is registered so the callback can consume it.
    assert "st-1" in calendar_oauth._OAUTH_STATES


async def test_auth_url_missing_secrets_returns_400(client):
    fake = MagicMock()
    fake.build_authorization_url.side_effect = FileNotFoundError("no client secrets")
    with _patch_gcal(auth=fake):
        resp = await client.get("/api/calendar/auth/url")
    assert resp.status_code == 400
    # Detail is a stable user-facing message — the raw exception text can
    # quote the path of the missing file (a server-side data-dir layout
    # detail) and must never reach the HTTP client.
    detail = resp.json()["detail"]
    assert "no client secrets" not in detail
    assert "client secrets" in detail


async def test_auth_url_unexpected_error_returns_500(client):
    fake = MagicMock()
    fake.build_authorization_url.side_effect = RuntimeError("boom")
    with _patch_gcal(auth=fake):
        resp = await client.get("/api/calendar/auth/url")
    assert resp.status_code == 500
    assert "oauth url error" in resp.json()["detail"]


# ── POST /api/calendar/auth/callback ───────────────────────────────────────


async def test_callback_exchanges_code(client):
    calendar_oauth._OAUTH_STATES.add("st-mint")
    fake = MagicMock()
    with _patch_gcal(auth=fake):
        resp = await client.post(
            "/api/calendar/auth/callback", json={"code": "abc", "state": "st-mint"}
        )
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}
    fake.exchange_code.assert_called_once_with("abc")


async def test_callback_valid_state_is_consumed(client):
    calendar_oauth._OAUTH_STATES.add("st-ok")
    fake = MagicMock()
    with _patch_gcal(auth=fake):
        resp = await client.post(
            "/api/calendar/auth/callback", json={"code": "abc", "state": "st-ok"}
        )
    assert resp.status_code == 200
    # A consumed state cannot be replayed.
    assert "st-ok" not in calendar_oauth._OAUTH_STATES


async def test_callback_invalid_state_returns_400(client):
    calendar_oauth._OAUTH_STATES.add("st-real")
    fake = MagicMock()
    with _patch_gcal(auth=fake):
        resp = await client.post(
            "/api/calendar/auth/callback", json={"code": "abc", "state": "st-forged"}
        )
    assert resp.status_code == 400
    assert resp.json()["detail"] == "invalid state"
    fake.exchange_code.assert_not_called()


async def test_callback_exchange_failure_returns_500(client):
    calendar_oauth._OAUTH_STATES.add("st-fail")
    fake = MagicMock()
    fake.exchange_code.side_effect = RuntimeError("token endpoint 400")
    with _patch_gcal(auth=fake):
        resp = await client.post(
            "/api/calendar/auth/callback", json={"code": "bad", "state": "st-fail"}
        )
    assert resp.status_code == 500
    assert "oauth callback error" in resp.json()["detail"]


# ── GET /api/calendar/auth/callback (browser redirect landing page) ────────


async def test_callback_get_success_renders_ok_page(client):
    calendar_oauth._OAUTH_STATES.add("st-get-ok")
    fake = MagicMock()
    with _patch_gcal(auth=fake):
        resp = await client.get(
            "/api/calendar/auth/callback",
            params={"code": "good", "state": "st-get-ok"},
        )
    assert resp.status_code == 200
    assert "now connected to Google Calendar" in resp.text
    fake.exchange_code.assert_called_once_with("good")


async def test_callback_get_access_denied_renders_403_recipe(client):
    resp = await client.get("/api/calendar/auth/callback", params={"error": "access_denied"})
    assert resp.status_code == 403
    assert "access_denied" in resp.text
    assert "Test users" in resp.text


async def test_callback_get_no_code_renders_400(client):
    resp = await client.get("/api/calendar/auth/callback")
    assert resp.status_code == 400
    assert "didn't receive an authorization code" in resp.text


async def test_callback_get_generic_error_renders_400(client):
    resp = await client.get("/api/calendar/auth/callback", params={"error": "server_error"})
    assert resp.status_code == 400
    assert "server_error" in resp.text


async def test_callback_get_state_mismatch_renders_400(client):
    calendar_oauth._OAUTH_STATES.add("st-real")
    fake = MagicMock()
    with _patch_gcal(auth=fake):
        resp = await client.get(
            "/api/calendar/auth/callback",
            params={"code": "good", "state": "st-forged"},
        )
    assert resp.status_code == 400
    assert "state mismatch" in resp.text
    fake.exchange_code.assert_not_called()


async def test_callback_get_valid_state_is_consumed(client):
    """A matching state is discarded so the redirect can't be replayed."""
    calendar_oauth._OAUTH_STATES.add("st-ok")
    fake = MagicMock()
    with _patch_gcal(auth=fake):
        resp = await client.get(
            "/api/calendar/auth/callback",
            params={"code": "good", "state": "st-ok"},
        )
    assert resp.status_code == 200
    assert "st-ok" not in calendar_oauth._OAUTH_STATES
    fake.exchange_code.assert_called_once_with("good")


async def test_callback_get_exchange_failure_renders_500(client):
    calendar_oauth._OAUTH_STATES.add("st-get-fail")
    fake = MagicMock()
    fake.exchange_code.side_effect = RuntimeError("network down")
    with _patch_gcal(auth=fake):
        resp = await client.get(
            "/api/calendar/auth/callback",
            params={"code": "good", "state": "st-get-fail"},
        )
    assert resp.status_code == 500
    assert "Could not exchange" in resp.text
    # The raw exception text must not leak into the OAuth landing page; only a
    # stable error code is shown so users have something actionable to report.
    assert "network down" not in resp.text
    assert "oauth_exchange_failed" in resp.text


# ── POST /api/calendar/secrets/upload ──────────────────────────────────────


async def test_secrets_upload_persists_installed_client_to_keychain(client):
    content = json.dumps({"installed": {"client_id": "cid", "client_secret": "secret"}})
    with patch("estormi_ingestion.google_calendar.auth.save_client_secrets") as save:
        resp = await client.post("/api/calendar/secrets/upload", json={"content": content})
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["client_type"] == "installed"
    assert body["stored"] == "keychain"
    # Forwarded to the keyring store with the uploaded JSON (it unwraps on save).
    save.assert_called_once()
    assert save.call_args[0][0] == {"installed": {"client_id": "cid", "client_secret": "secret"}}


async def test_secrets_upload_writes_no_cleartext_file(client, tmp_path, monkeypatch):
    """The client secret goes to the keyring only — never a cleartext file in
    the data dir."""
    monkeypatch.setenv("ESTORMI_DATA_DIR", str(tmp_path))
    content = json.dumps({"installed": {"client_id": "cid", "client_secret": "secret"}})
    resp = await client.post("/api/calendar/secrets/upload", json={"content": content})
    assert resp.status_code == 200
    assert not (tmp_path / "google_client_secrets.json").exists()


async def test_secrets_upload_forwards_latest_client(client):
    """Each upload forwards its client to the keyring store (last write wins)."""
    with patch("estormi_ingestion.google_calendar.auth.save_client_secrets") as save:
        await client.post(
            "/api/calendar/secrets/upload",
            json={"content": json.dumps({"installed": {"client_id": "stale", "client_secret": "x"}})},
        )
        resp = await client.post(
            "/api/calendar/secrets/upload",
            json={"content": json.dumps({"installed": {"client_id": "fresh", "client_secret": "y"}})},
        )
    assert resp.status_code == 200
    assert save.call_count == 2
    assert save.call_args[0][0]["installed"]["client_id"] == "fresh"


async def test_secrets_upload_rejects_invalid_json(client):
    resp = await client.post("/api/calendar/secrets/upload", json={"content": "{not json"})
    assert resp.status_code == 400
    # Detail is redacted (no raw parser text echoed back); see calendar_oauth.py.
    assert resp.json()["detail"] == "invalid OAuth client JSON"


async def test_secrets_upload_rejects_non_object(client):
    resp = await client.post("/api/calendar/secrets/upload", json={"content": json.dumps([1, 2])})
    assert resp.status_code == 400
    assert "JSON object" in resp.json()["detail"]


async def test_secrets_upload_rejects_missing_credentials(client):
    resp = await client.post(
        "/api/calendar/secrets/upload",
        json={"content": json.dumps({"installed": {"client_id": "only-id"}})},
    )
    assert resp.status_code == 400
    assert "Google OAuth client JSON" in resp.json()["detail"]


async def test_secrets_upload_rejects_web_client(client):
    """A 'Web application' client must be refused — Estormi needs Desktop."""
    content = json.dumps({"web": {"client_id": "cid", "client_secret": "secret"}})
    resp = await client.post("/api/calendar/secrets/upload", json={"content": content})
    assert resp.status_code == 400
    assert "Desktop app" in resp.json()["detail"]


# ── POST /api/calendar/auth/open ───────────────────────────────────────────


async def test_auth_open_launches_browser(client):
    fake = MagicMock()
    fake.build_authorization_url.return_value = ("https://accounts.google.com/o", "st-9")
    with _patch_gcal(auth=fake), patch("subprocess.Popen") as popen:
        resp = await client.post("/api/calendar/auth/open")
    assert resp.status_code == 200
    body = resp.json()
    assert body["opened"] is True
    assert body["state"] == "st-9"
    assert "st-9" in calendar_oauth._OAUTH_STATES
    popen.assert_called_once_with(["open", "https://accounts.google.com/o"])


async def test_auth_open_browser_launch_failure(client):
    fake = MagicMock()
    fake.build_authorization_url.return_value = ("https://accounts.google.com/o", "st-x")
    with (
        _patch_gcal(auth=fake),
        patch("subprocess.Popen", side_effect=OSError("no open binary")),
    ):
        resp = await client.post("/api/calendar/auth/open")
    assert resp.status_code == 200
    body = resp.json()
    assert body["opened"] is False
    assert body["error"] == "could not open browser"


async def test_auth_open_missing_secrets_returns_400(client):
    fake = MagicMock()
    fake.build_authorization_url.side_effect = FileNotFoundError("missing secrets")
    with _patch_gcal(auth=fake):
        resp = await client.post("/api/calendar/auth/open")
    assert resp.status_code == 400
    detail = resp.json()["detail"]
    assert detail == "Google client secrets are not configured"
    # The raw FileNotFoundError text (which carries the data-dir path) must not
    # leak to the caller — mirrors the hardened GET /api/calendar/auth/url.
    assert "missing secrets" not in detail


async def test_auth_open_unexpected_error_returns_500(client):
    fake = MagicMock()
    fake.build_authorization_url.side_effect = RuntimeError("kaput")
    with _patch_gcal(auth=fake):
        resp = await client.post("/api/calendar/auth/open")
    assert resp.status_code == 500
    assert "oauth open error" in resp.json()["detail"]


# ── DELETE /api/calendar/auth ──────────────────────────────────────────────


async def test_auth_delete_drops_token_and_sync_token(client, db):
    await db.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?)",
        ("google_calendar_sync_token", '{"cal": "tok"}'),
    )
    await db.commit()

    fake = MagicMock()
    with _patch_gcal(auth=fake):
        resp = await client.delete("/api/calendar/auth")
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}
    fake.delete_token.assert_called_once()

    cur = await db.execute(
        "SELECT value FROM settings WHERE key = ?", ("google_calendar_sync_token",)
    )
    assert await cur.fetchone() is None
    await cur.close()


# ── POST /api/google-calendar/sync-token/reset ─────────────────────────────


async def test_sync_token_reset_deletes_row(client, db):
    await db.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?)",
        ("google_calendar_sync_token", '{"cal": "tok"}'),
    )
    await db.commit()

    resp = await client.post("/api/google-calendar/sync-token/reset")
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}

    cur = await db.execute(
        "SELECT value FROM settings WHERE key = ?", ("google_calendar_sync_token",)
    )
    assert await cur.fetchone() is None
    await cur.close()


# ── GET /api/google-calendar/calendars ─────────────────────────────────────


async def test_list_calendars_unauthenticated_returns_401(client):
    fake_auth = MagicMock()
    fake_auth.get_credentials.return_value = None
    with _patch_gcal(auth=fake_auth):
        resp = await client.get("/api/google-calendar/calendars")
    assert resp.status_code == 401
    assert resp.json()["detail"] == "not authenticated"


async def test_list_calendars_returns_calendar_rows(client, db):
    await db.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?)",
        ("google_calendar_selected_ids", json.dumps(["cal-a"])),
    )
    await db.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?)",
        ("google_calendar_group_types", json.dumps({"cal-a": "work"})),
    )
    await db.commit()

    fake_auth = MagicMock()
    fake_auth.get_credentials.return_value = MagicMock()
    fake_sync = MagicMock()
    fake_sync._build_service.return_value = MagicMock()
    fake_sync._list_user_calendars.return_value = [
        {"id": "cal-a", "summary": "Work", "backgroundColor": "#fff"},
        {"id": "cal-b", "summary": "Personal"},
    ]
    with _patch_gcal(auth=fake_auth, sync=fake_sync):
        resp = await client.get("/api/google-calendar/calendars")
    assert resp.status_code == 200
    rows = resp.json()
    by_id = {r["id"]: r for r in rows}
    assert by_id["cal-a"]["selected"] is True
    assert by_id["cal-a"]["group_type"] == "work"
    # cal-b is not in the selected set → not selected; default group_type.
    assert by_id["cal-b"]["selected"] is False
    assert by_id["cal-b"]["group_type"] == "unknown"
    assert by_id["cal-b"]["name"] == "Personal"


async def test_list_calendars_tolerates_corrupt_settings_rows(client, db):
    """Unparseable / wrong-typed settings blobs degrade to empty maps —
    every calendar comes back selected with an ``unknown`` group_type."""
    await db.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?)",
        ("google_calendar_selected_ids", "{not json"),
    )
    await db.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?)",
        # Unparseable JSON — exercises the except (ValueError, TypeError) path.
        ("google_calendar_group_types", "{broken"),
    )
    await db.commit()

    fake_auth = MagicMock()
    fake_auth.get_credentials.return_value = MagicMock()
    fake_sync = MagicMock()
    fake_sync._build_service.return_value = MagicMock()
    fake_sync._list_user_calendars.return_value = [{"id": "cal-a", "summary": "Work"}]
    with _patch_gcal(auth=fake_auth, sync=fake_sync):
        resp = await client.get("/api/google-calendar/calendars")
    assert resp.status_code == 200
    row = resp.json()[0]
    # Empty selected set → default-selected; empty group map → unknown.
    assert row["selected"] is True
    assert row["group_type"] == "unknown"


async def test_list_calendars_tolerates_wrong_typed_group_map(client, db):
    """A group-types row holding valid JSON of the wrong shape (a list, not
    a dict) degrades to an empty map rather than raising."""
    await db.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?)",
        ("google_calendar_group_types", json.dumps(["not", "a", "dict"])),
    )
    await db.commit()

    fake_auth = MagicMock()
    fake_auth.get_credentials.return_value = MagicMock()
    fake_sync = MagicMock()
    fake_sync._build_service.return_value = MagicMock()
    fake_sync._list_user_calendars.return_value = [{"id": "cal-a", "summary": "Work"}]
    with _patch_gcal(auth=fake_auth, sync=fake_sync):
        resp = await client.get("/api/google-calendar/calendars")
    assert resp.status_code == 200
    assert resp.json()[0]["group_type"] == "unknown"


async def test_list_calendars_google_api_error_returns_502(client):
    fake_auth = MagicMock()
    fake_auth.get_credentials.return_value = MagicMock()
    fake_sync = MagicMock()
    fake_sync._build_service.side_effect = RuntimeError("403 from google")
    with _patch_gcal(auth=fake_auth, sync=fake_sync):
        resp = await client.get("/api/google-calendar/calendars")
    assert resp.status_code == 502
    # Detail is generic (the raw upstream text is logged server-side, not echoed).
    assert resp.json()["detail"] == "Google Calendar API request failed"


# ── PATCH /api/google-calendar/calendars/{id} ──────────────────────────────


async def test_patch_calendar_selects(client, db):
    # Seed a non-empty (explicit) selection so selecting another calendar
    # appends to it. Selecting from the empty "all selected" sentinel is a
    # no-op — see test_patch_calendar_select_from_sentinel_is_noop.
    await db.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?)",
        ("google_calendar_selected_ids", json.dumps(["cal-z"])),
    )
    await db.commit()

    resp = await client.patch("/api/google-calendar/calendars/cal-a", json={"selected": True})
    assert resp.status_code == 200
    assert resp.json()["selected"] is True

    cur = await db.execute(
        "SELECT value FROM settings WHERE key = ?", ("google_calendar_selected_ids",)
    )
    row = await cur.fetchone()
    await cur.close()
    assert json.loads(row[0]) == ["cal-z", "cal-a"]


async def test_patch_calendar_select_from_sentinel_is_noop(client, db):
    """Selecting a calendar while the stored set is empty (the "all selected"
    sentinel) must NOT materialize ``[calendar_id]`` — that would silently
    deselect every other calendar. The set stays empty (still all-selected)."""
    resp = await client.patch("/api/google-calendar/calendars/cal-a", json={"selected": True})
    assert resp.status_code == 200

    cur = await db.execute(
        "SELECT value FROM settings WHERE key = ?", ("google_calendar_selected_ids",)
    )
    row = await cur.fetchone()
    await cur.close()
    assert json.loads(row[0]) == []


async def test_patch_calendar_deselects(client, db):
    await db.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?)",
        ("google_calendar_selected_ids", json.dumps(["cal-a", "cal-b"])),
    )
    await db.commit()

    resp = await client.patch("/api/google-calendar/calendars/cal-a", json={"selected": False})
    assert resp.status_code == 200

    cur = await db.execute(
        "SELECT value FROM settings WHERE key = ?", ("google_calendar_selected_ids",)
    )
    row = await cur.fetchone()
    await cur.close()
    assert json.loads(row[0]) == ["cal-b"]


async def test_patch_calendar_first_deselect_materializes_full_set(client, db):
    """Deselecting the first calendar from the empty ("all selected") sentinel
    must persist every other calendar id, not an empty list — otherwise the
    deselection is silently lost (empty still reads as "all selected")."""
    fake_auth = MagicMock()
    fake_auth.get_credentials.return_value = MagicMock()
    fake_sync = MagicMock()
    fake_sync._build_service.return_value = MagicMock()
    fake_sync._list_user_calendars.return_value = [
        {"id": "cal-a"},
        {"id": "cal-b"},
        {"id": "cal-c"},
    ]
    with _patch_gcal(auth=fake_auth, sync=fake_sync):
        resp = await client.patch("/api/google-calendar/calendars/cal-b", json={"selected": False})
    assert resp.status_code == 200

    cur = await db.execute(
        "SELECT value FROM settings WHERE key = ?", ("google_calendar_selected_ids",)
    )
    row = await cur.fetchone()
    await cur.close()
    assert json.loads(row[0]) == ["cal-a", "cal-c"]


async def test_patch_calendar_sets_group_type(client, db):
    resp = await client.patch("/api/google-calendar/calendars/cal-a", json={"group_type": "family"})
    assert resp.status_code == 200
    assert resp.json()["group_type"] == "family"

    cur = await db.execute(
        "SELECT value FROM settings WHERE key = ?", ("google_calendar_group_types",)
    )
    row = await cur.fetchone()
    await cur.close()
    assert json.loads(row[0]) == {"cal-a": "family"}


async def test_patch_calendar_rejects_invalid_group_type(client):
    resp = await client.patch("/api/google-calendar/calendars/cal-a", json={"group_type": "bogus"})
    assert resp.status_code == 422
    assert "invalid group_type" in resp.json()["detail"]


async def test_patch_calendar_path_with_slashes(client, db):
    """Calendar ids contain ``@`` and sometimes path-like segments — the
    ``{calendar_id:path}`` converter must keep the full id intact."""
    # Seed a non-empty selection so the select appends the id (from the empty
    # sentinel a select is a no-op).
    await db.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?)",
        ("google_calendar_selected_ids", json.dumps(["seed"])),
    )
    await db.commit()
    cal_id = "team/shared@group.calendar.google.com"
    resp = await client.patch(f"/api/google-calendar/calendars/{cal_id}", json={"selected": True})
    assert resp.status_code == 200
    cur = await db.execute(
        "SELECT value FROM settings WHERE key = ?", ("google_calendar_selected_ids",)
    )
    row = await cur.fetchone()
    await cur.close()
    assert cal_id in json.loads(row[0])
