"""Static-asset mounts and ``/favicon.ico`` for the Estormi MCP server.

Encapsulates the asset surfaces that ``main.py`` used to wire up inline:
the brand asset directory, the source-icons directory, the favicon
endpoint, and the SPA bundle at ``/app``. The mounts are registered by
``register_static_mounts(app)`` which mirrors the original boot order so
reverse-route lookups behave identically.
"""

from __future__ import annotations

import mimetypes
import os
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from starlette.types import Scope

from .jobs import ROOT

# Location of the built SPA — ``packages/web-ui/dist`` — served at ``/app/``
# and opened by the tray "E" left-click. This is the only UI Estormi ships;
# the legacy 6-page shell was retired in favour of the compact one-pager.
_SPA_DIST = ROOT / "packages" / "web-ui" / "dist"

# Vendored webfonts. The directory is populated by
# ``scripts/vendor_fonts.py`` and committed to the repo so a fresh
# checkout serves them without a build step. See
# ``assets/fonts/SOURCE.md``.
_FONTS_DIR = ROOT / "assets" / "fonts"


class _ImmutableStaticFiles(StaticFiles):
    """``StaticFiles`` that sets aggressive caching headers.

    Webfont binaries never change at runtime — once a build is shipped
    every ``/fonts/<name>.woff2`` byte is content-addressed by the
    filename. We can therefore advertise the strongest possible cache:
    one year, immutable. This avoids re-downloading ~230 KB of fonts
    on every page load during a long-running Estormi session.
    """

    async def get_response(self, path: str, scope: Scope):  # type: ignore[override]
        response = await super().get_response(path, scope)
        # Only stamp 200/206 responses — 404s should not be cached.
        if response.status_code in (200, 206):
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        return response


# Defense-in-depth CSP for the SPA document. The Tauri WebView navigates from
# the tauri:// splash to the FastAPI origin on launch, after which the
# tauri.conf CSP no longer applies — so without this header the SPA would run
# with no CSP at all. This mirrors the Tauri policy: the bundled module script
# is same-origin (no inline scripts), inline styles are required (briefing HTML
# + React), fetch/SSE are same-origin. It blocks any inline <script> and remote
# image/beacon a crafted briefing-HTML body might smuggle through
# dangerouslySetInnerHTML — the briefing is already server-side HTML-escaped, so
# this is the documented second layer.
_SPA_CSP = (
    "default-src 'self'; "
    "script-src 'self'; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data:; "
    "font-src 'self' data:; "
    "connect-src 'self'; "
    "object-src 'none'; "
    "base-uri 'self'; "
    "frame-ancestors 'none'"
)


class _SpaStaticFiles(StaticFiles):
    """``StaticFiles`` tuned for the Vite SPA bundle at ``/app``.

    Vite emits content-hashed filenames for every JS/CSS asset
    (``index-<hash>.js``) and a top-level ``index.html`` that references
    them. That gives us two cache classes:

      * ``assets/*``  — hash-addressed → safe to cache forever.
      * ``index.html`` (and anything else at the root) — MUST revalidate
        on every load, otherwise the Tauri WebKit cache serves a stale
        page that references the previous build's hashed assets even
        after a fresh ``make bundle``.
    """

    async def get_response(self, path: str, scope: Scope):  # type: ignore[override]
        response = await super().get_response(path, scope)
        if response.status_code in (200, 206):
            if path.startswith("assets/"):
                response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
            else:
                # index.html + any other non-hashed root file.
                response.headers["Cache-Control"] = "no-cache, must-revalidate"
                # Stamp the CSP on the SPA document so the policy survives the
                # WebView's navigation off the tauri:// splash to this origin.
                response.headers["Content-Security-Policy"] = _SPA_CSP
        return response


def register_static_mounts(app: FastAPI) -> None:
    """Mount the static asset directories on ``app``.

    Safe to call once during module initialisation. Each mount is guarded
    by an ``os.path.isdir`` check so a partial install does not crash the
    server — the routes will simply 404 until the assets are installed.
    """

    # ── SPA (@estormi/web-ui) ──────────────────────────────────────────
    # The compact one-pager is the only UI Estormi ships; ``/`` redirects
    # here when the bundle is present.
    if _SPA_DIST.is_dir():
        # ``html=True`` makes the StaticFiles mount fall back to index.html
        # on cold loads. ``_SpaStaticFiles`` adds per-path cache headers so the
        # hashed assets cache forever but ``index.html`` always re-validates.
        app.mount("/app", _SpaStaticFiles(directory=str(_SPA_DIST), html=True), name="spa")

        @app.get("/app", include_in_schema=False)
        async def _spa_root() -> FileResponse:
            # The no-slash /app path is served by this handler, not the mount,
            # so it must stamp the CSP itself — otherwise a direct GET /app
            # returns the SPA document with no policy (the /app/ mount applies
            # it, but the bare URL is what a human types).
            return FileResponse(
                str(_SPA_DIST / "index.html"),
                media_type="text/html",
                headers={
                    "Cache-Control": "no-cache, must-revalidate",
                    "Content-Security-Policy": _SPA_CSP,
                },
            )
    else:
        # The SPA hasn't been built (fresh source checkout — ``web-ui/dist`` is
        # gitignored). Without this branch ``/app`` and ``/app/`` would 404 with
        # no explanation, which is exactly what a stranger following the
        # "Build from source" docs used to hit. Serve an actionable hint
        # instead of a bare 404. (Both the no-slash and slashed paths.)
        async def _spa_not_built() -> HTMLResponse:
            return HTMLResponse(
                "<!doctype html><meta charset=utf-8>"
                "<title>Estormi — SPA not built</title>"
                "<body style='font-family:system-ui;max-width:40rem;margin:4rem auto;padding:0 1rem'>"
                "<h1>The Ars Memoriae SPA isn't built yet</h1>"
                "<p>The web UI is served from <code>packages/web-ui/dist/</code>, "
                "which isn't present in a fresh source checkout. Build it once:</p>"
                "<pre style='background:#f4f4f4;padding:1rem;border-radius:6px'>make frontend-build</pre>"
                "<p>then reload this page. (<code>make bundle</code> and "
                "<code>make dev</code> build it automatically.)</p>"
                "</body>",
                status_code=503,
            )

        app.add_api_route("/app", _spa_not_built, include_in_schema=False)
        app.add_api_route("/app/", _spa_not_built, include_in_schema=False)

    _brand_dir = os.getenv("BRAND_DIR", str(ROOT / "assets/brand"))
    if os.path.isdir(_brand_dir):
        mimetypes.add_type("font/ttf", ".ttf")
        mimetypes.add_type("image/svg+xml", ".svg")
        app.mount("/brand", StaticFiles(directory=_brand_dir), name="brand")

    # ── Vendored webfonts ──────────────────────────────────────────────
    # Served at ``/fonts/<name>.woff2`` with a year-long ``immutable``
    # cache. The directory ships with the repo; if a fresh checkout
    # hasn't run ``scripts/vendor_fonts.py`` yet, the mount simply
    # 404s and the SPA falls back to system serifs via the @font-face
    # ``font-display: swap`` rule.
    _fonts_dir = os.getenv("FONTS_DIR", str(_FONTS_DIR))
    if os.path.isdir(_fonts_dir):
        mimetypes.add_type("font/woff2", ".woff2")
        mimetypes.add_type("font/woff", ".woff")
        app.mount(
            "/fonts",
            _ImmutableStaticFiles(directory=_fonts_dir),
            name="fonts",
        )

    _source_icons_dir = os.getenv("SOURCE_ICONS_DIR", str(ROOT / "assets/source-icons"))
    if os.path.isdir(_source_icons_dir):
        app.mount(
            "/source-icons",
            StaticFiles(directory=_source_icons_dir),
            name="source-icons",
        )

    _favicon_path = Path(_brand_dir) / "estormi-mark-32.png"

    @app.get("/favicon.ico", include_in_schema=False)
    async def favicon():
        if _favicon_path.exists():
            return FileResponse(str(_favicon_path), media_type="image/png")
        raise HTTPException(status_code=404, detail="favicon not found")


def is_spa_available() -> bool:
    """Whether the built SPA bundle is present on disk.

    ``/`` redirects to ``/app`` when this is true; on a source checkout that
    hasn't been built yet it 404s instead.
    """

    return _SPA_DIST.is_dir() and (_SPA_DIST / "index.html").is_file()
