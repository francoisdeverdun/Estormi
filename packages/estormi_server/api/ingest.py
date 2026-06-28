"""REST shim for the ingestion pipelines: ``POST /ingest_chunk``.

This route is the plain-HTTP equivalent of the ``ingest_chunk`` MCP tool
and is the entrypoint used by every shell-script ingestion stage. It
delegates to ``writers.ingest_chunk`` and sanitises any exception text
before returning it to the client (the raw exception can carry absolute
paths or SQL fragments).
"""

from __future__ import annotations

import structlog
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from estormi_server.server.limiter import limiter
from estormi_server.storage.writers import MAX_TEXT_LEN
from estormi_server.storage.writers import delete_by_source_id as _delete_by_source_id
from estormi_server.storage.writers import ingest_chunk as _ingest_chunk

log = structlog.get_logger()

router = APIRouter()


class IngestBody(BaseModel):
    # 1 MiB ceiling: real chunks land at < 50 KB after sanitisation. Higher
    # than the embedding-model context window, low enough that an abusive
    # caller can't drive sqlite + Qdrant into RAM exhaustion.
    text: str = Field(..., max_length=MAX_TEXT_LEN)
    source: str = Field(..., max_length=64)
    source_id: str = Field(default="", max_length=512)
    title: str = Field(default="", max_length=512)
    date: str = Field(default="", max_length=64)
    url: str = Field(default="", max_length=2048)
    content_hash: str = Field(..., max_length=128)
    # Optional explicit corpus override. Matches the MCP ``ingest_chunk`` tool
    # (the two transports are documented as equivalent); when omitted the server
    # derives it from the source name (``world`` for news/rss/youtube, else
    # ``personal``). First-party connectors rely on that derivation.
    corpus: str | None = Field(default=None, max_length=16)
    group_type: str | None = Field(default=None, max_length=64)
    pending_reply: bool = False
    chat_id_raw: str | None = Field(default=None, max_length=512)
    chat_name: str | None = Field(default=None, max_length=512)
    end_date_ts: str | None = Field(default=None, max_length=64)
    # Structured calendar event facts the Briefing reads as fields rather than
    # parsing the chunk text: event_type (default/outOfOffice/focusTime),
    # event_status (confirmed/tentative), and the day's working_location label.
    event_type: str | None = Field(default=None, max_length=64)
    event_status: str | None = Field(default=None, max_length=64)
    working_location: str | None = Field(default=None, max_length=256)
    # Optional connector-side hints. ``pii_filtered: True`` tells the server
    # the caller already ran ``filter_pii`` so we don't redact twice.
    meta: dict | None = None


# Loopback, first-party bulk endpoint with client-side backoff
# (estormi_ingestion/shared/http_client.post_chunk). The cap is a runaway-loop backstop,
# NOT an abuse gate — a full resync (e.g. a fresh DB re-pulling a calendar of
# recurring events) must drain in one window rather than exhaust the connector's
# retries, drop its sync token, and re-pull forever. See ingest_delete below.
@router.post("/ingest_chunk")
@limiter.limit("6000/minute")
async def ingest_chunk_rest(
    request: Request,
    body: IngestBody,
):
    try:
        return await _ingest_chunk(
            text=body.text,
            source=body.source,
            title=body.title,
            date=body.date,
            url=body.url,
            content_hash=body.content_hash,
            source_id=body.source_id,
            corpus=body.corpus,
            group_type=body.group_type,
            pending_reply=body.pending_reply,
            chat_id_raw=body.chat_id_raw,
            chat_name=body.chat_name,
            end_date_ts=body.end_date_ts,
            event_type=body.event_type,
            event_status=body.event_status,
            working_location=body.working_location,
            meta=body.meta,
        )
    except Exception:
        log.exception("ingest_chunk.error")
        return JSONResponse({"error": "ingest failed (see server logs)"}, status_code=500)


class IngestBatchBody(BaseModel):
    chunks: list[IngestBody] = Field(..., max_length=100)


@router.post("/ingest_batch")
@limiter.limit("200/minute")
async def ingest_batch_rest(request: Request, body: IngestBatchBody):
    results = []
    for chunk in body.chunks:
        try:
            result = await _ingest_chunk(
                text=chunk.text,
                source=chunk.source,
                title=chunk.title,
                date=chunk.date,
                url=chunk.url,
                content_hash=chunk.content_hash,
                source_id=chunk.source_id,
                corpus=chunk.corpus,
                group_type=chunk.group_type,
                pending_reply=chunk.pending_reply,
                chat_id_raw=chunk.chat_id_raw,
                chat_name=chunk.chat_name,
                end_date_ts=chunk.end_date_ts,
                event_type=chunk.event_type,
                event_status=chunk.event_status,
                working_location=chunk.working_location,
                meta=chunk.meta,
            )
            results.append(result)
        except Exception:
            log.exception(
                "ingest_batch.chunk_error", source=chunk.source, source_id=chunk.source_id
            )
            results.append({"error": "ingest failed", "source_id": chunk.source_id})
    return {"results": results, "count": len(results)}


class IngestDeleteBody(BaseModel):
    source: str = Field(..., max_length=64)
    source_id: str = Field(..., max_length=512)


# Same rationale as ingest_chunk: a recurring-event cleanup retracts many
# instances at once, so the cap must outlast a full resync, not throttle the
# loopback connector into failure.
@router.post("/ingest_delete")
@limiter.limit("6000/minute")
async def ingest_delete_rest(request: Request, body: IngestDeleteBody):
    """Retract a single item by (source, source_id) — SQLite + Qdrant.

    Connector-facing counterpart of ``/ingest_chunk``: lets an ingestion
    stage remove an item that disappeared upstream (e.g. a cancelled
    Google Calendar event) without the caller knowing the chunk id.
    """
    try:
        return await _delete_by_source_id(body.source, body.source_id)
    except Exception:
        log.exception("ingest_delete.error")
        return JSONResponse({"error": "delete failed (see server logs)"}, status_code=500)
