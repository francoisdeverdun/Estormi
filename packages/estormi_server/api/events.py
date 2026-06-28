"""SSE endpoint streaming engine lifecycle events to the SPA.

The SPA's ``LiveIndicator`` mirrors ``server.events``' in-process state.
Each connected client receives an ``engine.snapshot`` first so it reconciles
on connect/reconnect, then a live stream of ``engine.started`` /
``engine.stopped``. The browser's EventSource auto-reconnects on transient
drops; the snapshot on reconnect re-syncs without polling.
"""

from __future__ import annotations

import json

from fastapi import APIRouter, Request
from sse_starlette.sse import EventSourceResponse

from estormi_server.server.events import subscribe
from estormi_server.server.limiter import limiter

router = APIRouter()


@router.get("/api/events")
@limiter.limit("30/minute")
async def engine_events(request: Request):
    async def stream():
        async for event in subscribe():
            if await request.is_disconnected():
                break
            yield {"event": event["type"], "data": json.dumps(event)}

    # ``ping`` makes sse-starlette emit a comment heartbeat every N seconds,
    # which both keeps idle connections alive and surfaces dead clients to us
    # (the underlying write fails, cancelling our generator and releasing the
    # subscriber queue). Without it a client that connects and goes silent
    # would hold its slot in ``_subscribers`` until the next engine event
    # arrived, leaking memory under SPA-reload churn.
    return EventSourceResponse(stream(), ping=15)
