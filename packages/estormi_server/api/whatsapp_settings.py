"""WhatsApp sidecar passthrough + chat-management HTTP routes.

Status / QR / reset proxies to the loopback sidecar plus the per-chat
listing, name resolution, title back-fill, group_type editor, and the
LLM auto-tagger. The SQL + business logic lives in
:mod:`estormi_server.services.whatsapp`; this module is the thin HTTP shell
(route decorators, rate limiting, request/response shaping).
"""

from __future__ import annotations

import asyncio
import json

import httpx
import structlog
from fastapi import APIRouter, Request
from fastapi.responses import Response
from pydantic import BaseModel

from estormi_server.api._validation import validate_group_type
from estormi_server.integrations.whatsapp_sidecar import SIDECAR_URL, sidecar_headers
from estormi_server.server.limiter import limiter
from estormi_server.server.sources import WA_DB_PATH
from estormi_server.services import whatsapp as whatsapp_svc

log = structlog.get_logger()

router = APIRouter()


# ── WhatsApp sidecar passthrough ─────────────────────────────────────────────


@router.get("/api/whatsapp/status")
@limiter.limit("60/minute")
async def whatsapp_status(request: Request):
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            r = await client.get(f"{SIDECAR_URL}/api/whatsapp/status", headers=sidecar_headers())
            return r.json()
    except (httpx.ConnectError, httpx.TimeoutException):
        return {"connected": False, "session_state": "UNAVAILABLE"}


@router.get("/api/whatsapp/qr.png")
@limiter.limit("60/minute")
async def whatsapp_qr(request: Request):
    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            r = await client.get(f"{SIDECAR_URL}/api/whatsapp/qr.png", headers=sidecar_headers())
            if r.status_code == 204:
                return Response(status_code=204)
            return Response(content=r.content, media_type="image/png")
    except (httpx.ConnectError, httpx.TimeoutException):
        return Response(status_code=503)


# Re-exported so callers that still import ``wipe_whatsapp_log`` from this module
# (e.g. ``api.admin``) and any test patching it keep working after the split.
wipe_whatsapp_log = whatsapp_svc.wipe_whatsapp_log


@router.post("/api/whatsapp/reset")
@limiter.limit("5/minute")
async def whatsapp_reset(request: Request):
    """Disconnect WhatsApp: forget the session and wipe its data for a clean re-pair.

    A bare ``DELETE FROM chunks`` data reset deliberately keeps ``wa.db`` (it's
    a credential, not ingested data), but that strands WhatsApp — an
    already-paired device never gets a fresh HistorySync, so message history
    can't come back without forgetting the session. This per-source Disconnect
    is that escape hatch and a full clean slate: drop the session store, the
    sticky pairing marker, the stale ``whatsapp_chats`` metadata, AND the message
    log + its derived chunks. A re-pair then re-enumerates chats (re-resolving
    names) and re-backfills history from scratch, with no stale rows left behind.
    Disconnect is the only place that wipes on its own — an *involuntary* unpair
    (WhatsApp dropping the link) leaves everything intact and just surfaces
    "Awaiting scan", so old history isn't lost to a transient disconnect.
    """
    for suffix in ("", "-shm", "-wal"):
        try:
            (WA_DB_PATH.parent / (WA_DB_PATH.name + suffix)).unlink(missing_ok=True)
        except OSError:
            pass
    # Drop the sticky pairing marker too — the sidecar reset below clears it
    # when reachable, but we may be running it cold (sidecar offline).
    try:
        WA_DB_PATH.with_name("wa.paired").unlink(missing_ok=True)
    except OSError:
        pass
    # Forget chat metadata (names + user group_type labels). It maps to the old
    # account; leaving it behind would show phantom chats with zero chunks and
    # mis-tag the re-paired account. Re-pairing re-enumerates and re-tags.
    try:
        from estormi_server.storage.tools import get_write_lock, sqlite_conn  # noqa: PLC0415

        db = sqlite_conn()
        # Leaf DELETE→commit span — serialise on the shared write lock
        # (wipe_whatsapp_log below takes it independently). See ``tools._write_lock``.
        async with get_write_lock():
            await db.execute("DELETE FROM whatsapp_chats")
            await db.commit()
        # Also wipe the message log + derived chunks so the re-pair starts from
        # a genuine clean slate (chat list cleared above). See wipe_whatsapp_log.
        await whatsapp_svc.wipe_whatsapp_log(db)
    except Exception:
        log.exception("whatsapp_reset.data_clear_failed")
    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            await client.post(f"{SIDECAR_URL}/api/whatsapp/reset", headers=sidecar_headers())
    except Exception:
        pass  # best-effort: sidecar reset is optional, may be offline
    return {"reset": True}


@router.get("/api/whatsapp/chats")
@limiter.limit("30/minute")
async def whatsapp_chats(request: Request):
    from estormi_server.storage.tools import sqlite_conn  # noqa: PLC0415

    return await whatsapp_svc.list_chats(sqlite_conn())


class _ResolveNamesBody(BaseModel):
    chat_ids: list[str] = []


@router.post("/api/whatsapp/resolve-names")
@limiter.limit("30/minute")
async def whatsapp_resolve_names(request: Request, body: _ResolveNamesBody):
    """Resolve + persist names for the given chat ids and return the map.

    The WhatsApp ingestor calls this for chats whose name it doesn't have yet,
    so a brand-new DM's first messages get the real contact name even though
    the chat-list enrichment hasn't created its row. Persisting the result
    keeps every later reader (the chat list, the briefing) consistent.
    """
    from estormi_server.storage.tools import sqlite_conn  # noqa: PLC0415

    return await whatsapp_svc.resolve_and_persist_names(sqlite_conn(), body.chat_ids)


@router.post("/api/whatsapp/backfill-titles")
@limiter.limit("12/minute")
async def whatsapp_backfill_titles(request: Request):
    """Re-title WhatsApp chunks left with a raw-JID title by an ingest-time race.

    A chunk's title is baked in when its window is first ingested; if the chat's
    name resolved only later (the metadata lagged behind ingestion), the chunk
    keeps a ``WhatsApp — <raw JID>`` title forever and the briefing renders it
    as "a contact". This finds those chunks and rewrites the title once a real
    name is available. Idempotent: a chunk already carrying a real name is not
    matched, so steady-state runs update nothing.
    """
    from estormi_server.storage.tools import sqlite_conn  # noqa: PLC0415

    updated = await whatsapp_svc.backfill_titles(sqlite_conn())
    return {"updated": updated}


class _WhatsAppChatTypeBody(BaseModel):
    group_type: str = "unknown"


@router.patch("/api/whatsapp/chats/{chat_id:path}")
@limiter.limit("30/minute")
async def update_whatsapp_chat_type(chat_id: str, request: Request, body: _WhatsAppChatTypeBody):
    from estormi_server.storage.tools import sqlite_conn  # noqa: PLC0415

    group_type = body.group_type
    validate_group_type(group_type, whatsapp_svc._WA_GROUP_TYPES)
    retagged = await whatsapp_svc.set_chat_group_type(sqlite_conn(), chat_id, group_type)
    return {"chat_id": chat_id, "group_type": group_type, "retagged": retagged}


# ── Auto-tag (LLM-classified group_type) ─────────────────────────────────────


@router.get("/api/whatsapp/chats/auto-tag/status")
@limiter.limit("120/minute")
async def whatsapp_chats_auto_tag_status(request: Request):
    """Read the in-memory progress of the most recent auto-tag run.

    Lets the SPA poll for completion without re-issuing the POST (which
    no-ops while ``running=True`` but doesn't surface progress otherwise).
    """
    return whatsapp_svc.autotag_status_payload()


@router.post("/api/whatsapp/chats/auto-tag")
@limiter.limit("2/minute")
async def whatsapp_chats_auto_tag(request: Request):
    """Kick off LLM classification of WhatsApp chat tags.

    Internal trigger: the WhatsApp ingestion script POSTs to this at the end
    of each run so newly-paired chats with fresh message text get classified
    automatically. Body (optional): ``{"only_unknown": bool}`` — default
    ``true``; pass ``false`` to re-classify every chat.
    """
    try:
        body = await request.json()
    except (json.JSONDecodeError, ValueError):
        body = {}
    # A client posting a JSON list or scalar (``[]``, ``true``, ``42``) would
    # otherwise crash ``body.get(...)`` with AttributeError; treat anything
    # that isn't an object as "no body" and apply defaults.
    if not isinstance(body, dict):
        body = {}
    only_unknown = bool(body.get("only_unknown", True))

    async with whatsapp_svc._autotag_lock:
        if whatsapp_svc._autotag_state.running:
            return whatsapp_svc.autotag_status_payload()
        whatsapp_svc.begin_autotag_run()

    # Fire-and-forget — but register the task with the server's strong-ref
    # set so it cannot be silently GC'd mid-run. Without this the task can
    # be collected while still pending and ``_autotag_state.running``
    # stays True forever, locking out future runs.
    from estormi_server.server.jobs import _track_background_task  # noqa: PLC0415

    _track_background_task(asyncio.create_task(whatsapp_svc.run_autotag(only_unknown)))
    return whatsapp_svc.autotag_status_payload()
