"""System-level endpoints: ``/health``, ``/api/open-url``.

The ``open-url`` endpoint is intentionally constrained to a frozen
allow-list of macOS System Settings panes — any value supplied by the UI
is compared exactly against the literal strings in
``_SYSTEM_SETTINGS_PANES`` before being passed to ``open(1)`` as a literal
argv element (no shell, no string interpolation). New panes must be added to
the set explicitly with a comment explaining why.
"""

from __future__ import annotations

import asyncio
import subprocess

import structlog
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from estormi_server.server.limiter import limiter

log = structlog.get_logger()

router = APIRouter()


# Closed allow-list of System Settings panes Estormi may ask macOS to open.
# Any value supplied by the UI is checked exactly against this set — the value
# only ever reaches open(1) as a literal allow-listed argv element, with no
# shell or string interpolation anywhere on this path.
_SYSTEM_SETTINGS_PANES: frozenset[str] = frozenset(
    {
        "x-apple.systempreferences:com.apple.preference.security?Privacy_Automation",
        "x-apple.systempreferences:com.apple.preference.security?Privacy_AllFiles",
        "x-apple.systempreferences:com.apple.preference.security?Privacy_FilesAndFolders",
        "x-apple.systempreferences:com.apple.preference.security?Privacy_RemovableVolumes",
        "x-apple.systempreferences:com.apple.preference.security?Privacy_Calendars",
        "x-apple.systempreferences:com.apple.preference.security?Privacy_Reminders",
        "x-apple.systempreferences:com.apple.preference.security?Privacy_Contacts",
        "x-apple.systempreferences:com.apple.preference.security?Privacy_Photos",
        "x-apple.systempreferences:com.apple.preference.security?Privacy_AppleEvents",
        "x-apple.systempreferences:com.apple.preference.security?Privacy",
        "x-apple.systempreferences:com.apple.preference.notifications",
    }
)


@router.post("/api/open-url")
@limiter.limit("10/minute")
async def open_url(request: Request):
    from memory_core.audit import log_security_decision  # noqa: PLC0415

    client_host = request.client.host if request.client else ""
    try:
        body = await request.json()
    except Exception:
        # best-effort: malformed request body is a client error, reported as 400
        return JSONResponse({"status": "error", "detail": "invalid json"}, status_code=400)
    url = body.get("url", "") if isinstance(body, dict) else ""
    if not isinstance(url, str) or url not in _SYSTEM_SETTINGS_PANES:
        log_security_decision(
            decision="reject",
            path="/api/open-url",
            client_host=client_host,
            reason="open_url_not_in_allowlist",
            method="POST",
        )
        return JSONResponse({"status": "error", "detail": "url not in allow-list"}, status_code=400)
    # url is now known to be a literal from the allow-list — pass it to open(1)
    # as a literal argv element (no shell), so nothing user-controlled is parsed.
    try:
        await asyncio.to_thread(subprocess.run, ["open", url], timeout=5, check=False)
        log_security_decision(
            decision="accept",
            path="/api/open-url",
            client_host=client_host,
            reason="open_url_allowlisted",
            method="POST",
        )
        return {"status": "ok"}
    except Exception:
        log.exception("open_url.failed", url=url)
        return JSONResponse({"status": "error", "detail": "open failed"}, status_code=500)


@router.get("/health")
async def health():
    checks: dict[str, str] = {}
    try:
        from estormi_server.storage import tools  # noqa: PLC0415

        db = tools.sqlite_conn()
        cur = await db.execute("SELECT 1")
        await cur.close()
        checks["sqlite"] = "ok"
    except Exception:  # noqa: BLE001
        checks["sqlite"] = "degraded"
    try:
        from estormi_server.storage import tools  # noqa: PLC0415

        cols = await tools._client().get_collections()
        checks["qdrant"] = "ok" if cols else "degraded"
    except Exception:  # noqa: BLE001
        checks["qdrant"] = "degraded"
    status = "ok" if all(v == "ok" for v in checks.values()) else "degraded"
    return JSONResponse({"status": status, **checks})
