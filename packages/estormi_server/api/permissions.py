"""Permission endpoints.

The macOS permission preflight (which fires each prompt at app startup,
attributed to Estormi) runs from ``server/lifespan.py`` directly via
``run_preflight`` — not over HTTP — so there is no SPA-facing preflight route.
The only permission endpoint the SPA calls is :func:`recheck_fda`, used by the
iMessage Full Disk Access onboarding to re-check a just-granted permission.

See ``server/permission_preflight.py`` for the probe/persist machinery.
"""

from __future__ import annotations

import asyncio

import structlog
from fastapi import APIRouter, Request

from estormi_server.server.limiter import limiter

log = structlog.get_logger()

router = APIRouter()


@router.post("/api/permissions/recheck-fda")
@limiter.limit("12/minute")
async def recheck_fda(request: Request) -> dict:
    """Re-check iMessage Full Disk Access from the FDA-holding main binary.

    Called when the user returns to Estormi from System Settings (window focus)
    so a just-granted FDA is detected live, without relaunching the app. The
    re-check asks the Tauri host over the loopback to re-snapshot chat.db (only
    the main binary holds FDA — the sidecar can't) — see
    :func:`server.permissions.recheck_full_disk_access`. Returns
    ``{"status": "authorized" | "manual" | "unavailable"}``.
    """
    from estormi_server.server.permissions import recheck_full_disk_access  # noqa: PLC0415

    status = await asyncio.to_thread(recheck_full_disk_access)
    return {"status": status}
