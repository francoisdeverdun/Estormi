"""Local LLM model status + download endpoints.

The download endpoint is exposed as a GET because EventSource only supports
GET; the SPA's ``ExtModelSettings`` opens it that way to stream progress
to the browser.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import structlog
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sse_starlette.sse import EventSourceResponse

from estormi_server.api._downloads import make_log_abandoned_download
from estormi_server.server.limiter import limiter

log = structlog.get_logger()

router = APIRouter()


def _infer_tier(filename: str) -> str:
    """Best-effort tier id from a resolved GGUF filename.

    Matches the filename against the catalog's known model files, so a re-added
    tier is recognised without editing this function. Falls back to the default
    tier (Ministral 3 14B) when nothing matches."""
    from memory_core.llm_local import MODEL_CATALOG, model_file_path  # noqa: PLC0415

    name = filename.lower()
    for tier in MODEL_CATALOG:
        if Path(model_file_path(tier)).name.lower() == name:
            return tier
    return "ministral3-14b"


@router.get("/api/model/status")
@limiter.limit("30/minute")
async def model_status(request: Request):
    from memory_core.llm_local import _model_path, is_loaded

    p = Path(await _model_path())
    return {
        "downloaded": p.exists(),
        "loaded": await is_loaded(),
        "path": str(p),
        "tier": _infer_tier(p.name),
        "size_bytes": p.stat().st_size if p.exists() else 0,
    }


@router.get("/api/model/catalog")
@limiter.limit("30/minute")
async def model_catalog(request: Request):
    """Every model in the catalog with install state + per-engine selection.

    The Maintenance UI renders this: which models are downloaded, which to
    offer for install, and which model the briefing engine currently uses.
    Models list is in catalog (display) order.
    """
    from memory_core.llm_local import (
        MODEL_CATALOG,
        engine_roles,
        model_file_path,
        role_default_tier,
        selected_tier_for,
    )

    models = []
    for tier, meta in MODEL_CATALOG.items():
        p = Path(model_file_path(tier))
        downloaded = p.exists()
        if meta.get("local_only") and not downloaded:
            # Produced on this machine (fused QLoRA distillation): there is
            # nothing the UI could offer to download, so the row only exists
            # once the file does.
            continue
        models.append(
            {
                "tier": tier,
                "label": meta["label"],
                "family": meta["family"],
                "min_ram_gb": meta["min_ram_gb"],
                "expected_bytes": meta["expected_bytes"],
                "downloaded": downloaded,
                "size_bytes": p.stat().st_size if downloaded else 0,
            }
        )
    selection = {role: await selected_tier_for(role) for role in engine_roles()}
    defaults = {role: role_default_tier(role) for role in engine_roles()}
    return {"models": models, "selection": selection, "defaults": defaults}


@router.post("/api/model/delete")
@limiter.limit("10/minute")
async def model_delete(request: Request, payload: dict[str, str]):
    """Delete a downloaded model GGUF to reclaim disk.

    A model is binary in the UI — installed or not — so the catalog offers
    Download when absent and Delete when present. ``tier`` is validated against
    the catalog (no path traversal: the path is derived from the tier, never
    from caller input). If the model being deleted is the one currently
    resident in the sidecar, it is unloaded first so inference isn't left
    pointing at an unlinked file.
    """
    from memory_core.llm_local import (
        MODEL_CATALOG,
        _model_path,
        is_loaded,
        model_file_path,
        unload,
    )

    tier = (payload or {}).get("tier", "")
    if tier not in MODEL_CATALOG:
        return JSONResponse({"error": "unknown tier"}, status_code=400)

    path = Path(model_file_path(tier))
    try:
        if await is_loaded() and Path(await _model_path()) == path:
            await unload()
    except Exception:  # noqa: BLE001 — unload is best-effort; deletion proceeds
        log.warning("model.delete.unload_failed", tier=tier)

    deleted = False
    if path.exists():
        try:
            path.unlink()
            deleted = True
        except OSError:
            log.exception("model.delete.failed")
            return JSONResponse({"error": "delete_failed (see server logs)"}, status_code=500)
    # Dropping the local quill resets its training: clear the distill adapter so
    # the next distillation starts clean-from-scratch and no orphan is left.
    if tier == "ministral3-14b-estormi":
        try:
            import shutil

            from estormi_distill.paths import adapters_dir

            shutil.rmtree(adapters_dir(), ignore_errors=True)
        except Exception:  # noqa: BLE001 — cleanup is best-effort
            log.warning("model.delete.adapter_cleanup_failed", tier=tier)
    return {"tier": tier, "deleted": deleted}


@router.get("/api/model/download")
@limiter.limit("2/minute")
async def model_download_get(request: Request):
    """EventSource endpoint for the UI (EventSource only supports GET).

    NOTE: this GET is intentionally state-changing — it kicks off a multi-GB
    model download and writes the GGUF to disk. It is a GET only because the
    SPA streams progress over EventSource, which cannot issue a POST. The CSRF
    gate (server/security.py) exempts GET as "not state-changing", so this
    endpoint is reachable without the X-Estormi-Origin header. The exposure is
    accepted because the server is loopback-only, the action is rate-limited
    (2/min), and the worst case is disk/bandwidth use with no data exfiltration
    (the response body is unreadable cross-origin).
    """
    from memory_core.llm_local import MODEL_CATALOG, _model_path, download_model

    # Expected size (bytes) for the progress estimate — sourced from the
    # single catalog so a new tier needs no change here.
    tier = request.query_params.get("tier", "ministral3-14b")
    if MODEL_CATALOG.get(tier, {}).get("local_only"):
        return JSONResponse(
            {"error": "local-only tier — produced on this machine, no download source"},
            status_code=400,
        )
    expected = MODEL_CATALOG.get(tier, {}).get("expected_bytes", 4_700_000_000)

    async def stream():
        yield {"data": json.dumps({"message": "Starting download…"})}
        model_path = Path(await _model_path(tier))
        if model_path.exists():
            yield {
                "data": json.dumps(
                    {
                        "status": "done",
                        "message": f"✓ Already downloaded: {model_path.name}",
                    }
                )
            }
            return
        # download_model streams into ``<final>.part`` and atomically renames
        # on completion. Watch whichever exists so the bar reflects real bytes
        # in flight — the previous code only watched the final filename, which
        # appears at the very end, so the bar sat at 0% for the whole download.
        part_path = model_path.with_name(model_path.name + ".part")
        dl_task = asyncio.ensure_future(download_model(tier))
        try:
            while not dl_task.done():
                await asyncio.sleep(3)
                cur = model_path if model_path.exists() else part_path
                if cur.exists():
                    size = cur.stat().st_size
                    pct = min(99, int(size / expected * 100))
                    yield {"data": json.dumps({"message": f"Downloading… {pct}%", "progress": pct})}
            result = dl_task.result()
            yield {
                "data": json.dumps({"status": "done", "message": f"✓ Ready: {Path(result).name}"})
            }
        except asyncio.CancelledError:
            # The SSE client disconnected — the generator is being torn down, but
            # the download keeps running in the background. Attach a callback so
            # its eventual result/exception is retrieved (no GC "exception was
            # never retrieved") and any failure is logged.
            dl_task.add_done_callback(
                make_log_abandoned_download("model.download.abandoned_failed")
            )
            raise
        except Exception:
            # Download errors can quote internal paths verbatim; log the detail
            # server-side and ship a stable code to the browser.
            log.exception("model.download.failed")
            yield {
                "data": json.dumps(
                    {"status": "error", "message": "Error: download_failed (see server logs)"}
                )
            }

    return EventSourceResponse(stream())


# The briefing runs both local quills (two-quills routing) plus the narration
# voice, so the Officina offers them as ONE turn-key resource instead of three
# separate model rows: the two LLM GGUFs + the Voxtral TTS snapshot. Keys stay
# in sync with MODEL_CATALOG / tts_local.
_BUNDLE_LLM_TIERS = ("ministral3-14b", "gemma4-12b")


@router.get("/api/model/bundle/download")
@limiter.limit("2/minute")
async def model_bundle_download(request: Request):
    """EventSource: download the whole briefing model set in one gesture.

    Pulls both local quills (Ministral 3 14B + Gemma 4 12B) and the Voxtral
    narration voice sequentially, reporting a single aggregate progress bar.
    GET-only because EventSource cannot POST — same accepted exposure as
    ``/api/model/download`` (loopback-only, rate-limited, no exfiltration).
    Each component is idempotent, so a partially-installed bundle resumes
    cleanly: already-present files are skipped.
    """
    from memory_core import tts_local
    from memory_core.llm_local import (
        MODEL_CATALOG,
        _model_path,
        download_model,
        model_file_path,
    )

    async def _llm_bytes(tier: str) -> int:
        p = Path(await _model_path(tier))
        cur = p if p.exists() else p.with_name(p.name + ".part")
        return cur.stat().st_size if cur.exists() else 0

    total = sum(
        MODEL_CATALOG.get(t, {}).get("expected_bytes", 0) for t in _BUNDLE_LLM_TIERS
    ) + tts_local.TTS_CATALOG.get(tts_local.DEFAULT_TTS_MODEL, {}).get("expected_bytes", 0)

    async def _downloaded_bytes() -> int:
        b = sum([await _llm_bytes(t) for t in _BUNDLE_LLM_TIERS])
        return b + tts_local.model_size_bytes()

    def _all_present() -> bool:
        llms = all(Path(model_file_path(t)).exists() for t in _BUNDLE_LLM_TIERS)
        return llms and tts_local.is_model_downloaded()

    async def stream():
        yield {"data": json.dumps({"message": "Starting download…"})}
        if _all_present():
            yield {"data": json.dumps({"status": "done", "message": "✓ Already downloaded"})}
            return

        async def _run_all() -> None:
            for tier in _BUNDLE_LLM_TIERS:
                await download_model(tier)  # no-op when the GGUF already exists
            if not tts_local.is_model_downloaded():
                await asyncio.to_thread(tts_local.download_model)

        dl_task = asyncio.ensure_future(_run_all())
        try:
            while not dl_task.done():
                await asyncio.sleep(3)
                have = await _downloaded_bytes()
                pct = min(99, int(have / total * 100)) if total else 0
                yield {"data": json.dumps({"message": f"Downloading… {pct}%", "progress": pct})}
            dl_task.result()
            yield {"data": json.dumps({"status": "done", "message": "✓ Briefing models ready"})}
        except asyncio.CancelledError:
            # SSE client disconnected — the download keeps running; retrieve its
            # eventual result so a failure is logged (no GC "never retrieved").
            dl_task.add_done_callback(
                make_log_abandoned_download("model.bundle.download.abandoned_failed")
            )
            raise
        except Exception:
            log.exception("model.bundle.download.failed")
            yield {
                "data": json.dumps(
                    {"status": "error", "message": "Error: download_failed (see server logs)"}
                )
            }

    return EventSourceResponse(stream())
