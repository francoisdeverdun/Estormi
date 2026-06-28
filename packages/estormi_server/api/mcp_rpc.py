"""MCP JSON-RPC + SSE transport endpoints.

Implements the subset of the MCP HTTP/SSE transport that Claude Desktop and
claude.ai use:

  - ``POST /mcp`` — JSON-RPC 2.0 (initialize, tools/list, tools/call)
  - ``GET  /sse`` — Server-Sent Events stream for push notifications

The ``TOOLS`` catalogue and the ``_dispatch_tool`` dispatcher both live here
because they are wholly owned by the MCP transport — the REST shims in
``api/ingest.py`` and ``api/search.py`` call the lower-level ``tools``
functions directly, so there is no shared dependency.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import jsonschema
import structlog
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse

from estormi_server import __version__
from estormi_server.server.limiter import limiter
from estormi_server.services.chunks import retrieve_chunk_texts
from estormi_server.storage.search_api import fetch_around as _fetch_around
from estormi_server.storage.search_api import search_memory as _search_memory
from estormi_server.storage.tools import sqlite_conn
from estormi_server.storage.writers import delete_by_source
from estormi_server.storage.writers import delete_chunk as _delete_chunk
from estormi_server.storage.writers import ingest_chunk as _ingest_chunk

log = structlog.get_logger()

router = APIRouter()


def _source_slugs() -> list[str]:
    """Read the canonical source slugs from the connector registry.

    The list used to be hand-maintained here and drifted out of sync with
    ``connectors/registry``. Deriving it at import time means a new
    connector lights up the MCP enum automatically. Sorted for stable output.
    """
    from estormi_server.server.jobs import _registry  # noqa: PLC0415

    return sorted({spec.name for spec in _registry.specs()})


def _source_titles() -> list[str]:
    """Human-readable connector names for the ``search_memory`` prose.

    Derived from the registry like ``_source_slugs`` so the description can't
    drift out of sync when a connector is added or removed.
    """
    from estormi_server.server.jobs import _registry  # noqa: PLC0415

    return [spec.title for spec in sorted(_registry.specs(), key=lambda s: s.name)]


_SOURCE_SLUGS = _source_slugs()
_SOURCE_TITLES = _source_titles()


# ─── MCP tool catalog ────────────────────────────────────────────────────

TOOLS: list[dict] = [
    {
        "name": "search_memory",
        "description": (
            "Search the primary user's local knowledge base semantically. Returns the "
            f"top-k most relevant chunks from these sources: {', '.join(_SOURCE_TITLES)}. "
            "For calendar and chat results, respect group_type (see the field below): "
            "me is the primary user's own events; partner is the spouse/partner's; couple "
            "concerns the household; family may concern the household rather than the user "
            "alone; work/friends/organisation/charity/sport scope the context; noise/unknown "
            "are low-confidence."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Natural language query"},
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 100,
                    "default": 10,
                },
                "source": {
                    "type": "string",
                    "enum": _SOURCE_SLUGS,
                    "description": "Optional source filter",
                },
                "after": {
                    "type": "string",
                    "description": "ISO-8601 lower bound on date_ts (e.g. 2025-01-01 or 2025-01-01T00:00:00Z)",
                },
                "before": {
                    "type": "string",
                    "description": "ISO-8601 upper bound on date_ts",
                },
                "group_type": {
                    "type": "string",
                    "enum": [
                        "me",
                        "partner",
                        "work",
                        "family",
                        "couple",
                        "friends",
                        "organisation",
                        "charity",
                        "sport",
                        "noise",
                        "unknown",
                    ],
                    "description": (
                        "Optional SEMANTIC life-context filter shared by WhatsApp chats and "
                        "calendars: me/partner/work/family/couple/friends/organisation/charity/"
                        "sport/noise/unknown. For the WhatsApp structural kind (dm/group/"
                        "broadcast) use 'chat_kind' instead — the two axes are independent. "
                        "Semantics: me is the primary user's own calendar; partner is the spouse/partner's "
                        "calendar; couple concerns the household; family may concern the household "
                        "rather than the primary user alone."
                    ),
                },
                "chat_kind": {
                    "type": "string",
                    "enum": ["dm", "group", "broadcast"],
                    "description": (
                        "Optional STRUCTURAL WhatsApp-chat filter, auto-derived from the JID: "
                        "dm (1:1 conversation), group, or broadcast. Independent of the semantic "
                        "'group_type' tag — a chat can be both e.g. group + work."
                    ),
                },
                "pending_reply": {
                    "type": "boolean",
                    "description": "If true, return only WhatsApp chunks where the primary user has not yet replied",
                },
                "sources": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Filter to multiple sources (OR). Overrides 'source' if both are given.",
                },
                "corpus": {
                    "type": "string",
                    "enum": ["personal", "world"],
                    "description": "Scope to the user's own memory ('personal') or world news/knowledge ('world'). Omit to search both.",
                },
                "min_score": {
                    "type": "number",
                    "minimum": 0.0,
                    "maximum": 1.0,
                    "description": (
                        "Absolute dense-cosine floor [0,1]. When set, search runs in "
                        "dense-only relatedness mode and returns only chunks whose cosine "
                        "clears it — use to answer 'is this actually related?' (~0.6 is a "
                        "good floor). Omit for normal hybrid (dense+BM25) ranking."
                    ),
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "fetch_around",
        "description": (
            "Time-window retrieval: return every chunk across ALL sources whose date overlaps a "
            "window centred on a date (± window_days), newest first. Use this to correlate — a mail, "
            "a calendar event, a reminder and a chat about the same thing cluster in time, so one "
            "window surfaces the whole thread. No semantic query needed."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "date": {
                    "type": "string",
                    "description": "Centre of the window, ISO-8601 (e.g. 2026-05-30 or 2026-05-30T00:00:00Z).",
                },
                "window_days": {
                    "type": "integer",
                    "minimum": 0,
                    "maximum": 90,
                    "default": 1,
                    "description": "Half-width of the window in days (the window spans date ± window_days).",
                },
                "forward_days": {
                    "type": "integer",
                    "minimum": 0,
                    "maximum": 90,
                    "description": (
                        "Optional independent look-ahead in days. Omit for a symmetric window "
                        "(forward == window_days). Set 0 to keep the window from crossing into "
                        "tomorrow — 'the centre day and the prior window_days', no next-day leak."
                    ),
                },
                "sources": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional restriction to a subset of sources (OR).",
                },
                "corpus": {
                    "type": "string",
                    "enum": ["personal", "world"],
                    "description": "Optional corpus scope: 'personal' or 'world'.",
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 500,
                    "default": 200,
                },
            },
            "required": ["date"],
        },
    },
    {
        "name": "ingest_chunk",
        "description": "Store a text chunk in the knowledge base (used by ingestion pipelines).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "text": {"type": "string"},
                "source": {"type": "string"},
                "source_id": {
                    "type": "string",
                    "description": "Stable upstream id (note id, message-id, event uid).",
                },
                "title": {"type": "string"},
                "date": {"type": "string"},
                "url": {"type": "string"},
                "content_hash": {"type": "string"},
                "group_type": {"type": "string"},
                "pending_reply": {"type": "boolean"},
                "chat_id_raw": {"type": "string"},
                "chat_name": {"type": "string"},
                "end_date_ts": {
                    "type": "string",
                    "description": "Optional end timestamp (ISO-8601) for time-windowed chunks (WhatsApp/iMessage rolling windows).",
                },
                "event_type": {
                    "type": "string",
                    "description": "Calendar event nature: default / outOfOffice / focusTime.",
                },
                "event_status": {
                    "type": "string",
                    "description": "Calendar event status: confirmed / tentative (a 'maybe' RSVP).",
                },
                "working_location": {
                    "type": "string",
                    "description": "The day's working-location label for calendar events, e.g. 'Home office'.",
                },
                "corpus": {
                    "type": "string",
                    "enum": ["personal", "world"],
                    "description": "Corpus tag; defaults to 'world' for news/rss/youtube sources, else 'personal'.",
                },
                "meta": {
                    "type": "object",
                    "description": "Connector hints, e.g. {pii_filtered: true} to skip re-redaction.",
                },
            },
            "required": ["text", "source", "content_hash"],
        },
    },
    {
        "name": "delete_by_source",
        "description": "Delete ALL chunks (and their vectors) for a given source. No undo short of re-ingestion.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "source": {
                    "type": "string",
                    "description": "Canonical source name (e.g. notes, mail, whatsapp).",
                },
            },
            "required": ["source"],
        },
    },
    {
        "name": "delete_chunk",
        "description": "Permanently delete a single chunk by its ID from the knowledge base.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "id": {"type": "string", "description": "Chunk UUID"},
            },
            "required": ["id"],
        },
    },
    {
        "name": "get_chunk",
        "description": "Fetch the full text and metadata of a chunk by its ID.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "id": {"type": "string", "description": "Chunk UUID"},
            },
            "required": ["id"],
        },
    },
]


_TOOL_SCHEMAS: dict[str, dict] = {t["name"]: t["inputSchema"] for t in TOOLS}


async def _dispatch_tool(name: str, arguments: dict) -> Any:
    if name == "search_memory":
        return await _search_memory(
            query=arguments["query"],
            limit=arguments.get("limit", 10),
            source_filter=arguments.get("source"),
            after=arguments.get("after"),
            before=arguments.get("before"),
            group_type=arguments.get("group_type"),
            chat_kind=arguments.get("chat_kind"),
            pending_reply=arguments.get("pending_reply"),
            sources=arguments.get("sources"),
            corpus=arguments.get("corpus"),
            min_score=arguments.get("min_score"),
        )
    if name == "fetch_around":
        return await _fetch_around(
            date=arguments["date"],
            window_days=arguments.get("window_days", 1),
            forward_days=arguments.get("forward_days"),
            sources=arguments.get("sources"),
            corpus=arguments.get("corpus"),
            limit=arguments.get("limit", 200),
        )
    if name == "ingest_chunk":
        return await _ingest_chunk(
            text=arguments["text"],
            source=arguments["source"],
            title=arguments.get("title", ""),
            date=arguments.get("date", ""),
            url=arguments.get("url", ""),
            content_hash=arguments["content_hash"],
            source_id=arguments.get("source_id", ""),
            group_type=arguments.get("group_type"),
            pending_reply=arguments.get("pending_reply", False),
            chat_id_raw=arguments.get("chat_id_raw"),
            chat_name=arguments.get("chat_name"),
            end_date_ts=arguments.get("end_date_ts"),
            event_type=arguments.get("event_type"),
            event_status=arguments.get("event_status"),
            working_location=arguments.get("working_location"),
            corpus=arguments.get("corpus"),
            meta=arguments.get("meta"),
        )
    if name == "delete_by_source":
        return await delete_by_source(arguments["source"])
    if name == "delete_chunk":
        return await _delete_chunk(arguments["id"])
    if name == "get_chunk":
        chunk_id = arguments["id"]
        db = sqlite_conn()
        cursor = await db.execute("SELECT * FROM chunks WHERE id = ?", (chunk_id,))
        row = await cursor.fetchone()
        await cursor.close()
        if not row:
            raise HTTPException(status_code=404, detail="chunk not found")
        # Chunk text lives in Qdrant, not the SQL row. Reuse the shared service
        # helper (batched, best-effort, empty on miss) rather than re-issuing the
        # retrieve here — it resolves the client lazily, so it honours the active
        # client and test patches regardless of import order.
        texts = await retrieve_chunk_texts([chunk_id])
        return {**dict(row), "text": texts.get(chunk_id, "")}
    raise HTTPException(status_code=404, detail=f"Unknown tool: {name}")


# ─── MCP JSON-RPC endpoint ───────────────────────────────────────────────


class JsonRpcRequest(BaseModel):
    jsonrpc: str = Field(default="2.0")
    id: int | str | None = None
    method: str
    params: dict = Field(default_factory=dict)


def _rpc_result(req_id: int | str | None, result: Any) -> dict:
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def _rpc_error(req_id: int | str | None, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}


@router.post("/mcp")
@limiter.limit("120/minute")
async def mcp_rpc(
    request: Request,
    rpc: JsonRpcRequest,
):
    method = rpc.method
    params = rpc.params or {}

    if method == "initialize":
        return _rpc_result(
            rpc.id,
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "estormi", "version": __version__},
            },
        )

    if method == "tools/list":
        return _rpc_result(rpc.id, {"tools": TOOLS})

    if method == "tools/call":
        # Coerce a missing/non-string name to "" so _dispatch_tool reports it as
        # an unknown tool (404) rather than this being a type hole.
        name = str(params.get("name") or "")
        arguments = params.get("arguments", {}) or {}

        # Validate arguments against the tool's inputSchema before dispatch.
        tool_schema = _TOOL_SCHEMAS.get(name)
        if tool_schema is not None:
            try:
                jsonschema.validate(arguments, tool_schema)
            except jsonschema.ValidationError as exc:
                return _rpc_error(rpc.id, -32602, f"Invalid params: {exc.message}")

        try:
            result = await _dispatch_tool(name, arguments)
        except HTTPException as exc:
            # _dispatch_tool raises HTTPException for known failure modes
            # (unknown tool / chunk not found → 404, unparseable required
            # 'date' → 400). MCP clients speak JSON-RPC, so surface these as
            # JSON-RPC errors rather than letting FastAPI turn them into a bare
            # HTTP 4xx response the client cannot parse. A 400/404 is a client
            # input fault → -32602 Invalid params; anything else is internal.
            code = -32602 if exc.status_code in (400, 404) else -32000
            return _rpc_error(rpc.id, code, str(exc.detail))
        except (KeyError, TypeError, ValueError) as exc:
            # _dispatch_tool reads arguments positionally (arguments["query"],
            # arguments["date"]), so a missing or wrong-typed required argument
            # raises one of these. That is a client params
            # fault, not an internal fault: map it to -32602 and skip the ERROR
            # traceback the genuine-fault path below logs.
            log.info("tool.invalid_params", tool=name, error=str(exc))
            # Don't echo the raw exception text — a ValueError/TypeError message
            # can carry internal state (paths, SQL fragments). The detail is in
            # the structured log above for operators; the client gets a static
            # params-fault marker, mirroring the generic-fault path below.
            return _rpc_error(rpc.id, -32602, "Invalid params (see server logs)")
        except Exception:
            log.exception("tool.error", tool=name)
            # Do not echo the raw exception (it can contain absolute paths,
            # SQL fragments, or other internal state). The full traceback is
            # in the structured log above for operators.
            return _rpc_error(rpc.id, -32000, f"Tool '{name}' failed (see server logs)")
        return _rpc_result(
            rpc.id,
            {
                "content": [{"type": "text", "text": json.dumps(result, default=str)}],
                "isError": False,
            },
        )

    return _rpc_error(rpc.id, -32601, f"Method not found: {method}")


# ─── SSE endpoint (keep-alive heartbeat) ────────────────────────────────


@router.get("/sse")
@limiter.limit("30/minute")
async def sse_endpoint(request: Request):
    async def event_stream():
        yield {"event": "ready", "data": json.dumps({"server": "estormi"})}
        while not await request.is_disconnected():
            await asyncio.sleep(15)
            yield {"event": "ping", "data": "{}"}

    return EventSourceResponse(event_stream())
