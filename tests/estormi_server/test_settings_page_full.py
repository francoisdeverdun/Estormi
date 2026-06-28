"""Tests for api/settings_ui.py — pure helpers, API routes, toggle/ingest.

This module used to test ``settings_page.py``, which was a thin shim that
re-exported the same handlers from ``api/settings_ui.py``. The shim has
been deleted as part of the Phase 3 SPA cleanup; tests now import the
canonical names directly.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from estormi_server.api.admin import admin_reset_settings
from estormi_server.api.overview import _dir_size, _fmt_bytes
from estormi_server.api.sources_admin import (
    _ToggleSourceBody,
    toggle_source,
)
from estormi_server.api.whatsapp_settings import whatsapp_qr, whatsapp_status

pytestmark = pytest.mark.integration

# ── _fmt_bytes (pure) ─────────────────────────────────────────────────────────


class TestFmtBytes:
    def test_bytes(self):
        assert _fmt_bytes(512) == "512 B"

    def test_kilobytes(self):
        assert "KB" in _fmt_bytes(1500)

    def test_megabytes(self):
        assert "MB" in _fmt_bytes(5 * 1024 * 1024)

    def test_gigabytes(self):
        assert "GB" in _fmt_bytes(3 * 1024**3)

    def test_zero(self):
        assert _fmt_bytes(0) == "0 B"


# ── _dir_size (filesystem) ───────────────────────────────────────────────────


class TestDirSize:
    def test_nonexistent(self, tmp_path):
        assert _dir_size(tmp_path / "nope") == 0

    def test_single_file(self, tmp_path):
        f = tmp_path / "a.txt"
        f.write_text("hello")
        assert _dir_size(f) == 5

    def test_directory(self, tmp_path):
        (tmp_path / "a.txt").write_text("hello")
        (tmp_path / "b.txt").write_text("world!")
        assert _dir_size(tmp_path) == 11


# ── toggle_source (DB-backed) ────────────────────────────────────────────────


class TestToggleSource:
    @pytest.fixture(autouse=True)
    async def _setup(self, db):
        self._db = db
        yield

    async def test_toggle_on(self):
        mock_req = MagicMock()
        body = _ToggleSourceBody(enabled=True)
        fake_perm = {"key": "AppleEvents:Notes", "status": "authorized"}
        # Stub the macOS permission probe — toggling a source on triggers
        # it, and we must not spawn osascript / EventKit during tests.
        with (
            patch("estormi_server.storage.tools.sqlite_conn", return_value=self._db),
            patch(
                "estormi_server.server.permissions.ensure_source_permission",
                return_value=fake_perm,
            ) as mock_perm,
        ):
            result = await toggle_source("notes", mock_req, body)
        assert result["enabled"] is True
        assert result["source"] == "notes"
        # Activation triggers + reports the source's macOS permission.
        assert result["permission"] == fake_perm
        # notes has no configured root, so the probe is called with root=None.
        mock_perm.assert_called_once_with("notes", None)
        cursor = await self._db.execute(
            "SELECT value FROM settings WHERE key = 'source_notes_enabled'"
        )
        row = await cursor.fetchone()
        assert row[0] == "true"

    async def test_toggle_off(self):
        mock_req = MagicMock()
        body = _ToggleSourceBody(enabled=False)
        # Disabling a source never probes permissions.
        with (
            patch("estormi_server.storage.tools.sqlite_conn", return_value=self._db),
            patch("estormi_server.server.permissions.ensure_source_permission") as mock_perm,
        ):
            result = await toggle_source("mail", mock_req, body)
        assert result["enabled"] is False
        assert result["permission"] is None
        mock_perm.assert_not_called()


# ── whatsapp_status ──────────────────────────────────────────────────────────


def _mock_httpx_client(responses):
    """Build an httpx.AsyncClient mock whose .get() returns given MagicMock responses in order."""

    mock_cls = MagicMock()
    ctx = AsyncMock()
    ctx.get = AsyncMock(side_effect=responses)
    mock_cls.return_value.__aenter__ = AsyncMock(return_value=ctx)
    mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)
    return mock_cls


def _httpx_response(status_code: int, content: bytes = b"", json_body=None):
    import httpx

    r = MagicMock(spec=httpx.Response)
    r.status_code = status_code
    r.content = content
    if json_body is not None:
        r.json = MagicMock(return_value=json_body)
    return r


class TestWhatsappStatus:
    async def test_sidecar_down(self):
        import httpx

        with patch("estormi_server.api.whatsapp_settings.httpx.AsyncClient") as mock_cls:
            ctx = AsyncMock()
            ctx.get = AsyncMock(side_effect=httpx.ConnectError("refused"))
            mock_cls.return_value.__aenter__ = AsyncMock(return_value=ctx)
            mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)
            result = await whatsapp_status(MagicMock())
        assert result["connected"] is False
        assert result["session_state"] == "UNAVAILABLE"

    async def test_paired(self):
        resp = _httpx_response(200, json_body={"connected": True, "session_state": "PAIRED"})
        with patch(
            "estormi_server.api.whatsapp_settings.httpx.AsyncClient", _mock_httpx_client([resp])
        ):
            result = await whatsapp_status(MagicMock())
        assert result["session_state"] == "PAIRED"
        assert result["connected"] is True

    async def test_unpaired(self):
        resp = _httpx_response(200, json_body={"connected": False, "session_state": "UNPAIRED"})
        with patch(
            "estormi_server.api.whatsapp_settings.httpx.AsyncClient", _mock_httpx_client([resp])
        ):
            result = await whatsapp_status(MagicMock())
        assert result["session_state"] == "UNPAIRED"

    async def test_timeout_returns_unavailable(self):
        import httpx

        with patch("estormi_server.api.whatsapp_settings.httpx.AsyncClient") as mock_cls:
            ctx = AsyncMock()
            ctx.get = AsyncMock(side_effect=httpx.TimeoutException("timed out"))
            mock_cls.return_value.__aenter__ = AsyncMock(return_value=ctx)
            mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)
            result = await whatsapp_status(MagicMock())
        assert result["session_state"] == "UNAVAILABLE"


# ── whatsapp_qr ───────────────────────────────────────────────────────────────


class TestWhatsappQr:
    async def test_returns_png_when_sidecar_returns_200(self):
        png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
        resp = _httpx_response(200, content=png)
        with patch(
            "estormi_server.api.whatsapp_settings.httpx.AsyncClient", _mock_httpx_client([resp])
        ):
            result = await whatsapp_qr(MagicMock())
        assert result.status_code == 200
        assert result.media_type == "image/png"
        assert result.body == png

    async def test_returns_204_when_sidecar_returns_204(self):
        """204 = QR not available (connected or not yet initialised).

        204 is the SPA's auto-poll branch; it MUST stay distinct from 200 so
        the SPA can branch on ``r.status`` (not ``r.ok``) — hence the empty
        body alongside the bare status code.
        """
        resp = _httpx_response(204)
        with patch(
            "estormi_server.api.whatsapp_settings.httpx.AsyncClient", _mock_httpx_client([resp])
        ):
            result = await whatsapp_qr(MagicMock())
        assert result.status_code == 204
        assert result.body in (b"", None)

    async def test_returns_503_when_sidecar_unreachable(self):
        import httpx

        with patch("estormi_server.api.whatsapp_settings.httpx.AsyncClient") as mock_cls:
            ctx = AsyncMock()
            ctx.get = AsyncMock(side_effect=httpx.ConnectError("refused"))
            mock_cls.return_value.__aenter__ = AsyncMock(return_value=ctx)
            mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)
            result = await whatsapp_qr(MagicMock())
        assert result.status_code == 503

    async def test_returns_503_on_timeout(self):
        import httpx

        with patch("estormi_server.api.whatsapp_settings.httpx.AsyncClient") as mock_cls:
            ctx = AsyncMock()
            ctx.get = AsyncMock(side_effect=httpx.TimeoutException("timed out"))
            mock_cls.return_value.__aenter__ = AsyncMock(return_value=ctx)
            mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)
            result = await whatsapp_qr(MagicMock())
        assert result.status_code == 503


# The wire-level QR contract the Vite SPA depends on — 200/204/503 returned
# distinctly so the SPA can branch on ``r.status`` (not ``r.ok``) — is pinned
# by ``TestWhatsappQr`` above; the old server-rendered-JS tests
# (``TestWhatsappQrJsLogic``) are gone with the server-side settings page,
# their behaviour now covered by the Vite tests in ``packages/web-ui/``.


# ── admin_reset_settings ──────────────────────────────────────────────────────


class TestAdminResetSettings:
    """POST /api/admin/reset-settings clears settings but keeps ingested data."""

    @pytest.fixture(autouse=True)
    async def _setup(self, db):
        self._db = db
        await db.execute(
            "INSERT INTO chunks (id, content_hash, source) VALUES ('c1', 'h1', 'notes')"
        )
        await db.execute("INSERT INTO settings (key, value) VALUES ('schedule_cron', '0 2 * * *')")
        await db.execute("INSERT INTO settings (key, value) VALUES ('setup_completed', '1')")
        await db.commit()
        yield

    async def test_clears_settings(self):
        mock_req = MagicMock()
        with patch("estormi_server.storage.tools.sqlite_conn", return_value=self._db):
            result = await admin_reset_settings(mock_req)

        assert result["status"] == "ok"
        cursor = await self._db.execute("SELECT COUNT(*) FROM settings")
        assert (await cursor.fetchone())[0] == 0

    async def test_preserves_chunks(self):
        mock_req = MagicMock()
        with patch("estormi_server.storage.tools.sqlite_conn", return_value=self._db):
            await admin_reset_settings(mock_req)

        cursor = await self._db.execute("SELECT COUNT(*) FROM chunks")
        assert (await cursor.fetchone())[0] == 1

    async def test_idempotent_on_empty_settings(self):
        mock_req = MagicMock()
        with patch("estormi_server.storage.tools.sqlite_conn", return_value=self._db):
            await admin_reset_settings(mock_req)
            result = await admin_reset_settings(mock_req)

        assert result["status"] == "ok"


# ── Reset endpoint contract ──────────────────────────────────────────────────
#
# Old test class TestResetButtonsUI asserted on inline JS in the
# server-rendered settings page. The Settings UI now lives in a Vite SPA
# and the JS is no longer part of the Python response. We instead pin
# the three admin endpoints' contracts (status + JSON shape + side
# effects) and confirm the SPA shell HTML is reachable.


class TestAdminResetEndpointsViaHTTP:
    """End-to-end check that the reset endpoints respond correctly
    over HTTP and (where appropriate) leave the right cleanup signal in
    the JSON body for the SPA to act on."""

    async def test_reset_all_endpoint_exists_and_returns_status_ok(self, client):
        resp = await client.post("/api/admin/reset")
        assert resp.status_code == 200
        assert resp.json().get("status") == "ok"

    async def test_reset_settings_endpoint_exists_and_returns_status_ok(self, client):
        resp = await client.post("/api/admin/reset-settings")
        assert resp.status_code == 200
        assert resp.json().get("status") == "ok"

    async def test_reset_settings_clears_setup_completed(self, client, db):
        """The SPA reads this signal to decide whether to render the
        ``#setup`` wizard page: after a settings reset, ``setup_completed``
        must be gone so the SPA can route the user back through the wizard."""
        resp = await client.post("/api/admin/reset-settings")
        assert resp.status_code == 200
        cursor = await db.execute("SELECT COUNT(*) FROM settings WHERE key='setup_completed'")
        assert (await cursor.fetchone())[0] == 0

    async def test_whatsapp_reset_data_keeps_log_clears_watermark(self, client, db):
        """The light WhatsApp reset drops chunks + the `whatsapp_log` watermark
        but KEEPS the durable message log, so chunks re-derive with no rescan."""
        await db.execute(
            "INSERT INTO whatsapp_messages (msg_id, chat_id, ts_iso, text) "
            "VALUES ('m1', 'c@g.us', '2026-06-01T10:00:00+00:00', 'hi')"
        )
        await db.execute(
            "INSERT INTO ingestion_watermarks (source, last_fetched_at) "
            "VALUES ('whatsapp_log', '2026-06-01T10:00:00+00:00')"
        )
        await db.commit()

        resp = await client.post("/api/sources/whatsapp/reset")
        assert resp.status_code == 200

        cur = await db.execute("SELECT COUNT(*) FROM whatsapp_messages")
        assert (await cur.fetchone())[0] == 1  # raw log kept
        cur = await db.execute(
            "SELECT COUNT(*) FROM ingestion_watermarks WHERE source='whatsapp_log'"
        )
        assert (await cur.fetchone())[0] == 0  # watermark cleared → re-derive

    async def test_whatsapp_log_reset_wipes_durable_log(self, client, db):
        """The heavy reset drops the raw message log too — needs a rescan."""
        await db.execute(
            "INSERT INTO whatsapp_messages (msg_id, chat_id, ts_iso, text) "
            "VALUES ('m1', 'c@g.us', '2026-06-01T10:00:00+00:00', 'hi')"
        )
        await db.execute(
            "INSERT INTO ingestion_watermarks (source, last_fetched_at) "
            "VALUES ('whatsapp_log', '2026-06-01T10:00:00+00:00')"
        )
        await db.commit()

        resp = await client.post("/api/sources/whatsapp/log/reset")
        assert resp.status_code == 200

        cur = await db.execute("SELECT COUNT(*) FROM whatsapp_messages")
        assert (await cur.fetchone())[0] == 0  # raw log wiped
        cur = await db.execute(
            "SELECT COUNT(*) FROM ingestion_watermarks WHERE source='whatsapp_log'"
        )
        assert (await cur.fetchone())[0] == 0
