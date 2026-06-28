"""Local TTS (voice) model catalog + download endpoints.

The voice counterpart to :mod:`api.model`. The Officina card renders the
catalog (install state) and streams a download over the EventSource endpoint —
exposed as GET because EventSource only supports GET, same as the LLM download.
One model ships today (Voxtral); the catalog shape lets the picker grow.
"""

from __future__ import annotations

import asyncio
import json

import structlog
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sse_starlette.sse import EventSourceResponse

from estormi_server.api._downloads import make_log_abandoned_download
from estormi_server.server.limiter import limiter

log = structlog.get_logger()

router = APIRouter()


@router.get("/api/tts/catalog")
@limiter.limit("30/minute")
async def tts_catalog(request: Request):
    """Every TTS model with install state, in catalog (display) order."""
    from estormi_server.storage.tools import sqlite_conn  # noqa: PLC0415
    from memory_core import tts_local

    models = []
    for key, meta in tts_local.TTS_CATALOG.items():
        # Install state is resolved globally because the whole tts_local module
        # ships a single model (one snapshot dir, one repo). The catalog is a
        # dict so the picker *lists* future voices without a UI change, but a
        # genuine second model must first give tts_local per-key directory
        # resolution (model_dir/download/delete are single-model today).
        downloaded = tts_local.is_model_downloaded()
        models.append(
            {
                "key": key,
                "label": meta["label"],
                "family": meta["family"],
                "min_ram_gb": meta["min_ram_gb"],
                "expected_bytes": meta["expected_bytes"],
                "downloaded": downloaded,
                "size_bytes": tts_local.model_size_bytes() if downloaded else 0,
            }
        )
    # The selected model mirrors the LLM picker's contract: the
    # briefing_tts_model setting wins when it names a catalog entry, else the
    # default. With one shipped model the choice is inert in synthesis, but
    # the picker persists it so a second model slots in without UI changes.
    db = sqlite_conn()
    cur = await db.execute("SELECT value FROM settings WHERE key = 'briefing_tts_model'")
    row = await cur.fetchone()
    await cur.close()
    stored = (row["value"] if row else "") or ""
    selected = stored if stored in tts_local.TTS_CATALOG else tts_local.DEFAULT_TTS_MODEL

    return {
        "models": models,
        "selected": selected,
        # Narrator presets for the Officina voice selector. The active choice
        # lives in settings (briefing_tts_voice; "" = match briefing language).
        "voices": sorted(tts_local.VALID_VOICES),
    }


@router.post("/api/tts/delete")
@limiter.limit("10/minute")
async def tts_delete(request: Request, payload: dict[str, str]):
    """Delete the downloaded TTS snapshot to reclaim disk.

    ``key`` is validated against the catalog (no path traversal — the directory
    is derived in ``tts_local``, never from caller input).
    """
    from memory_core import tts_local

    key = (payload or {}).get("key", "")
    if key not in tts_local.TTS_CATALOG:
        return JSONResponse({"error": "unknown key"}, status_code=400)

    try:
        deleted = await asyncio.to_thread(tts_local.delete_model)
    except OSError:
        log.exception("tts.delete.failed")
        return JSONResponse({"error": "delete_failed (see server logs)"}, status_code=500)
    return {"key": key, "deleted": deleted}


@router.get("/api/tts/download")
@limiter.limit("2/minute")
async def tts_download_get(request: Request):
    """EventSource endpoint that streams Voxtral snapshot download progress.

    GET-only because EventSource cannot POST; like ``/api/model/download`` this
    GET is intentionally state-changing. Accepted because the server is
    loopback-only, the action is rate-limited (2/min), and the worst case is
    disk/bandwidth use with no data exfiltration.
    """
    from memory_core import tts_local

    key = request.query_params.get("key", tts_local.DEFAULT_TTS_MODEL)
    expected = tts_local.TTS_CATALOG.get(key, {}).get("expected_bytes", 2_500_000_000)

    async def stream():
        yield {"data": json.dumps({"message": "Starting download…"})}
        if tts_local.is_model_downloaded():
            yield {"data": json.dumps({"status": "done", "message": "✓ Already downloaded"})}
            return
        # snapshot_download is synchronous (CDN-bound) — run it off the event
        # loop and poll the snapshot dir's growing size for the progress bar.
        dl_task = asyncio.ensure_future(asyncio.to_thread(tts_local.download_model))
        try:
            while not dl_task.done():
                await asyncio.sleep(3)
                size = tts_local.model_size_bytes()
                if size:
                    pct = min(99, int(size / expected * 100))
                    yield {"data": json.dumps({"message": f"Downloading… {pct}%", "progress": pct})}
            dl_task.result()
            yield {"data": json.dumps({"status": "done", "message": "✓ Ready"})}
        except asyncio.CancelledError:
            # SSE client disconnected — the download keeps running. Attach a
            # callback so its result/exception is retrieved (no GC "exception
            # was never retrieved") and any failure is logged.
            dl_task.add_done_callback(make_log_abandoned_download("tts.download.abandoned_failed"))
            raise
        except Exception:
            log.exception("tts.download.failed")
            yield {
                "data": json.dumps(
                    {"status": "error", "message": "Error: download_failed (see server logs)"}
                )
            }

    return EventSourceResponse(stream())
