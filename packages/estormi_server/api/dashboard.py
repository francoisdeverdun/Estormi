"""Root route left over after the SPA migration.

History
-------
This module used to render a fleet of legacy server-side HTML pages —
``/``, ``/dashboard``, ``/memory``, ``/sources``, ``/docs``, ``/briefing``,
``/entities`` — by delegating to page modules that have since been deleted
now that the Vite SPA at ``packages/web-ui`` is the canonical UI.

What stays here:
  - ``GET /`` → 307 to ``/app/`` when the SPA bundle is on disk. On a source
    checkout without a built bundle, the static-asset mount in
    ``server/static.py`` simply won't expose ``/app/``, so this route 404s —
    the expected behaviour pending a future source-checkout fallback page.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from estormi_server.server.limiter import limiter
from estormi_server.server.static import is_spa_available

router = APIRouter()


@router.get("/", response_class=HTMLResponse)
@limiter.limit("60/minute")
async def home(request: Request):  # noqa: ARG001 — Request needed for slowapi
    if is_spa_available():
        return RedirectResponse(url="/app/", status_code=307)
    raise HTTPException(
        status_code=404,
        detail=(
            "SPA bundle not found at packages/web-ui/dist. "
            "Run `pnpm --filter @estormi/web-ui build` from the repo root."
        ),
    )
