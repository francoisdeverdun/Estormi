"""Smoke tests for the Settings page in the Ars Memoriae SPA.

The Settings page used to be its own Vite bundle with its own ``/settings``
FastAPI route. That is gone now: Settings is one of the SPA pages in
``packages/web-ui/dist`` and is reached via ``/app/#settings``.

These tests pin the wire-level contract a freshly-launched Tauri shell
depends on:

  - the SPA bundle is reachable at ``/app/``
  - ``GET /`` redirects there when the bundle is built
  - the settings-overview JSON aggregate the page consumes is healthy
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[2]
SPA_DIST = REPO_ROOT / "packages" / "web-ui" / "dist"

_SPA_MISSING_REASON = "SPA bundle not built — run `pnpm --filter @estormi/web-ui build` first"


def _require_spa_bundle() -> None:
    """Skip on developer laptops, hard-fail in CI.

    A green CI run without the SPA bundle is a silent gap: the Settings page
    contract isn't actually being verified. We detect CI via ``CI=true``
    (the standard GitHub Actions / GitLab signal) and fail loudly there;
    locally we keep the skip so a fresh clone runs cleanly without the JS
    build step.
    """
    if (SPA_DIST / "index.html").exists():
        return
    if os.environ.get("CI") == "true":
        pytest.fail(_SPA_MISSING_REASON)
    pytest.skip(_SPA_MISSING_REASON)


class TestSpaShell:
    async def test_root_redirects_to_spa_when_built(self, client):
        """When the SPA bundle is on disk, ``GET /`` MUST 307 to ``/app/``.
        Without the bundle the route returns 404 so source checkouts know
        they need to run the build."""
        _require_spa_bundle()
        resp = await client.get("/", follow_redirects=False)
        assert resp.status_code == 307
        assert resp.headers["location"] == "/app/"

    async def test_spa_index_served(self, client):
        _require_spa_bundle()
        resp = await client.get("/app/")
        assert resp.status_code == 200
        assert "text/html" in resp.headers["content-type"]


class TestSettingsOverviewEndpoint:
    """``GET /api/settings/overview`` is the aggregate JSON the SPA
    fetches on load. It replaces the server-side ``_build_page`` data
    assembly that used to live in ``settings_page.py``."""

    async def test_overview_returns_expected_top_level_keys(self, client):
        resp = await client.get("/api/settings/overview")
        assert resp.status_code == 200
        data = resp.json()
        for key in (
            "version",
            "data_dir",
            "settings",
            "storage",
            "sources",
            "model",
            "mcp",
            "pipeline",
            "whatsapp",
            "knowledge_sources",
        ):
            assert key in data, f"settings overview missing key: {key}"

    async def test_overview_storage_shape(self, client):
        resp = await client.get("/api/settings/overview")
        storage = resp.json()["storage"]
        assert {
            "db_bytes",
            "qdrant_bytes",
            "staging_bytes",
            "whatsapp_cache_bytes",
            "total_chunks",
        } <= storage.keys()
        for k, v in storage.items():
            assert isinstance(v, int), f"storage.{k} should be int, got {type(v)}"

    async def test_overview_mcp_shape(self, client):
        resp = await client.get("/api/settings/overview")
        mcp = resp.json()["mcp"]
        assert {"port", "bind_address"} <= mcp.keys()
        # The keychain bearer token is deliberately NOT surfaced — the snapshot
        # is served to any loopback caller without auth and the SPA never reads
        # it, so echoing it would leak the secret. See api/overview.py.
        assert "token" not in mcp
        # Defaults from a fresh settings table.
        assert mcp["port"] == "8000"
        assert mcp["bind_address"] == "127.0.0.1"

    async def test_overview_sources_shape(self, client):
        resp = await client.get("/api/settings/overview")
        sources = resp.json()["sources"]
        assert "counts" in sources and isinstance(sources["counts"], dict)
        assert "watermarks" in sources and isinstance(sources["watermarks"], dict)


class TestSettingsCsrfContract:
    """Every state-changing /api/ call from the SPA carries the
    ``X-Estormi-Origin: tauri`` header. Confirm the server rejects
    state-changing calls that omit it."""

    async def test_put_settings_without_csrf_origin_is_403(self, client):
        # Drop both CSRF header AND bearer so the CSRF gate fires.
        resp = await client.put(
            "/api/settings",
            json={"schedule_cron": "0 2 * * *"},
            headers={"X-Estormi-Origin": "", "Authorization": ""},
        )
        assert resp.status_code == 403
        assert "X-Estormi-Origin" in resp.text or "Origin" in resp.text

    async def test_post_sources_toggle_without_csrf_origin_is_403(self, client):
        resp = await client.post(
            "/api/sources/notes/toggle",
            json={"enabled": True},
            headers={"X-Estormi-Origin": "", "Authorization": ""},
        )
        assert resp.status_code == 403

    async def test_settings_overview_get_allowed_without_csrf(self, client):
        """GETs are safe — they don't need the CSRF stamp."""
        resp = await client.get(
            "/api/settings/overview",
            headers={"X-Estormi-Origin": ""},
        )
        assert resp.status_code == 200
