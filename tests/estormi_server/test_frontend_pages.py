"""Frontend integration tests — surviving routes for the SPA.

The legacy server-rendered pages (``/dashboard``, ``/memory``, ``/sources``,
``/briefing``, ``/entities``, ``/settings``, ``/setup``, ``/docs``) are gone.
The compact one-pager SPA at ``packages/web-ui/dist`` covers everything,
reached at ``/app/``.

This file pins:
  - the surviving root redirect to the SPA when the bundle is built
  - the JSON endpoints the SPA depends on
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.integration

# ── Root redirect ────────────────────────────────────────────────────────────


class TestRootRoute:
    async def test_root_behaviour(self, client):
        """``/`` redirects to ``/app/`` when the SPA bundle is built, or
        404s with an instructive message otherwise. Both are correct."""
        from estormi_server.server.static import is_spa_available

        resp = await client.get("/", follow_redirects=False)
        if is_spa_available():
            assert resp.status_code in (302, 307)
            assert resp.headers["location"].startswith("/app")
        else:
            assert resp.status_code == 404


# ── SPA Content-Security-Policy ───────────────────────────────────────────────


class TestSpaCsp:
    async def test_spa_document_carries_csp_on_every_entry(self, client):
        """The SPA document must carry _SPA_CSP however it is reached — the
        ``/app/`` mount AND the bare ``/app`` handler (the no-slash URL a human
        types). The CSP is the second layer behind the briefing's server-side
        HTML escaping; a no-CSP entry would silently drop that defense."""
        from estormi_server.server.static import is_spa_available

        if not is_spa_available():
            pytest.skip("SPA bundle not built")
        for path in ("/app/", "/app"):
            resp = await client.get(path, follow_redirects=False)
            assert resp.status_code == 200, path
            csp = resp.headers.get("content-security-policy", "")
            assert "script-src 'self'" in csp, f"{path} missing script-src"
            assert "img-src 'self' data:" in csp, f"{path} missing img-src"


# ── API JSON endpoints (pipeline, settings) ──────────────────────────────────


class TestApiJsonEndpoints:
    async def test_api_pipeline(self, client):
        resp = await client.get("/api/pipeline")
        assert resp.status_code == 200

    async def test_api_knowledge_sources(self, client):
        """The knowledge-sources section of the Settings page reads this."""
        resp = await client.get("/api/knowledge/sources")
        assert resp.status_code == 200
        assert isinstance(resp.json(), list)


# ── 404 sanity ───────────────────────────────────────────────────────────────


class TestUnknownRoutes:
    async def test_nonexistent_route_404(self, client):
        resp = await client.get("/this-does-not-exist-xyz")
        assert resp.status_code == 404

    async def test_legacy_dashboard_route_gone(self, client):
        """``/dashboard`` was an HTML route that the SPA replaced."""
        resp = await client.get("/dashboard")
        assert resp.status_code == 404

    async def test_legacy_entities_route_gone(self, client):
        resp = await client.get("/entities")
        assert resp.status_code == 404

    async def test_legacy_settings_route_gone(self, client):
        resp = await client.get("/settings")
        assert resp.status_code == 404

    async def test_legacy_setup_route_gone(self, client):
        resp = await client.get("/setup")
        assert resp.status_code == 404
