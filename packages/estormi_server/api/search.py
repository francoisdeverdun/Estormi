"""REST shims for retrieval: ``POST /search_memory`` and ``POST /fetch_around``.

Plain-HTTP equivalents of the ``search_memory`` / ``fetch_around`` MCP tools,
used by the local dashboard, the briefing engine, and anything that prefers
REST to JSON-RPC. The request bodies mirror the MCP tools' ``inputSchema``
(see ``api.mcp_rpc.TOOLS``).
"""

from __future__ import annotations

import structlog
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from estormi_server.server.limiter import limiter
from estormi_server.storage.search_api import fetch_around as _fetch_around
from estormi_server.storage.search_api import search_memory as _search_memory

log = structlog.get_logger()

router = APIRouter()


class SearchBody(BaseModel):
    query: str = Field(..., min_length=1, max_length=2000)
    limit: int = Field(default=10, ge=1, le=100)
    source: str | None = None
    after: str | None = None
    before: str | None = None
    group_type: str | None = None
    chat_kind: str | None = None
    pending_reply: bool | None = None
    sources: list[str] | None = None
    corpus: str | None = None
    # Absolute dense-cosine floor [0, 1]. When set, search runs in
    # dense-only relatedness mode (see search_api.search_memory) and returns
    # only chunks with cosine >= min_score. The briefing's event correlation
    # uses ~0.6; left unset, search stays hybrid RRF.
    min_score: float | None = Field(default=None, ge=0.0, le=1.0)


@router.post("/search_memory", response_model=None)
@limiter.limit("120/minute")
async def search_memory_rest(
    request: Request,
    body: SearchBody,
) -> list[dict] | JSONResponse:
    try:
        return await _search_memory(
            query=body.query,
            limit=body.limit,
            source_filter=body.source,
            after=body.after,
            before=body.before,
            group_type=body.group_type,
            chat_kind=body.chat_kind,
            pending_reply=body.pending_reply,
            sources=body.sources,
            corpus=body.corpus,
            min_score=body.min_score,
        )
    except HTTPException:
        # Deliberate client-error contract (e.g. an unparseable required
        # parameter → 400). Let FastAPI render it; do not mask as a 500.
        raise
    except Exception:
        log.exception("search_memory.error")
        return JSONResponse({"error": "search failed (see server logs)"}, status_code=500)


class FetchAroundBody(BaseModel):
    date: str = Field(..., min_length=4, max_length=40)
    # Cap at 90: the briefing's correlation anchor (BRIEFING_CORRELATION_HORIZON_DAYS,
    # default 75) needs a ~2-month forward window — a tighter cap silently 422s it.
    window_days: int = Field(default=1, ge=0, le=90)
    # Optional independent look-ahead. None = symmetric (forward == window_days);
    # 0 keeps the window from crossing into tomorrow (see day_context).
    forward_days: int | None = Field(default=None, ge=0, le=90)
    sources: list[str] | None = None
    corpus: str | None = None
    limit: int = Field(default=200, ge=1, le=500)


@router.post("/fetch_around", response_model=None)
@limiter.limit("120/minute")
async def fetch_around_rest(
    request: Request,
    body: FetchAroundBody,
) -> list[dict] | JSONResponse:
    try:
        return await _fetch_around(
            date=body.date,
            window_days=body.window_days,
            forward_days=body.forward_days,
            sources=body.sources,
            corpus=body.corpus,
            limit=body.limit,
        )
    except HTTPException:
        # Deliberate client-error contract: ``fetch_around`` raises
        # HTTPException(400) for an unparseable required ``date`` (see U18).
        # Let FastAPI render it; do not mask as a 500.
        raise
    except Exception:
        log.exception("fetch_around.error")
        return JSONResponse({"error": "search failed (see server logs)"}, status_code=500)
