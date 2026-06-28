"""Distillation engine endpoints — status for the Maintenance card, run to
enqueue the chain.

The card is a thin view over two files the engine owns (``distill/status.json``
and the reference workspace) plus the tooling probe — no distillation logic
lives in the server process.
"""

from __future__ import annotations

import structlog
from fastapi import APIRouter, Request
from sse_starlette.sse import EventSourceResponse

from estormi_server.server import jobs
from estormi_server.server.limiter import limiter

router = APIRouter()
log = structlog.get_logger(__name__)


@router.get("/api/distill/status")
@limiter.limit("30/minute")
async def distill_status(request: Request):
    """Everything the Maintenance card renders: workspace, tooling, verdicts.

    ``references`` counts the briefings harvested from the vault archive into
    the refs workspace; ``references.models`` splits them into hand-corrected
    (``user-edited``) and untouched (``archive``) so the card can surface the
    human-curated share.
    """
    import shutil
    from pathlib import Path

    from estormi_distill.paths import MIN_FREE_GB, distill_dir, read_status
    from estormi_distill.references import (
        MIN_BRIEFINGS,
        existing_references,
        vault_briefing_count,
    )
    from estormi_distill.trainer import INSTALLED_GGUF, tooling
    from memory_core.llm_local import model_file_path

    refs = existing_references()
    models: dict[str, int] = {}
    for meta in refs.values():
        key = meta.get("model") or "?"
        models[key] = models.get(key, 0) + 1
    installed = Path(model_file_path("ministral3-14b-estormi"))
    # Free space on the *workspace* volume (the dir may not exist yet — probe the
    # nearest existing ancestor) so the card can warn before a run aborts.
    wsp = distill_dir()
    probe = wsp
    while not probe.exists() and probe.parent != probe:
        probe = probe.parent
    try:
        free_gb = round(shutil.disk_usage(probe).free / 2**30, 1)
    except OSError:
        free_gb = None
    status = read_status()
    # The card's "active" flag keys off this list, so it must include the engine
    # *executing right now* (tracked by the event bus), not only the pending
    # queue: during a solo run the queue is empty, so queue_snapshot() alone
    # would read as idle and the card would never show a run as active.
    from estormi_server.server import events as engine_events  # noqa: PLC0415

    running = jobs.queue_snapshot()
    current = engine_events.current_kind()
    if current:
        running = [{"kind": current, "source": "running"}, *running]
    return {
        "status": status,
        "references": {
            "days": sorted(refs),
            "count": len(refs),
            "vaultCount": vault_briefing_count(),
            "minBriefings": MIN_BRIEFINGS,
            "models": models,
        },
        "tooling": tooling(),
        "installed": installed.exists(),
        "installedFile": INSTALLED_GGUF,
        "workspace": {"dir": str(wsp), "freeGb": free_gb, "needGb": MIN_FREE_GB},
        "running": running,
    }


@router.get("/api/distill/log")
@limiter.limit("30/minute")
async def distill_log(request: Request, lines: int = 150):
    """Tail the distillation engine log for the engine-room log view.

    Mirrors ``/api/knowledge/log``: the distill chain writes one file
    (``<data dir>/logs/distill.log``), so there is no per-stage selection.
    """
    import asyncio  # noqa: PLC0415

    from estormi_server.server.launchers.distill import _DISTILL_LOG  # noqa: PLC0415

    # Clamp the tail length so a caller can't ask splitlines() to materialise an
    # unbounded list.
    lines = max(1, min(lines, 5000))
    try:
        if not _DISTILL_LOG.exists():
            return {"content": "(log not found — distillation has not run yet)"}
        from estormi_server.server.log_tail import tail_lines  # noqa: PLC0415

        tail = await asyncio.to_thread(tail_lines, str(_DISTILL_LOG), lines)
        return {"content": tail}
    except Exception:
        log.exception("distill.log.read_failed")
        from fastapi.responses import JSONResponse  # noqa: PLC0415

        return JSONResponse({"error": "distill log read failed"}, status_code=500)


@router.post("/api/distill/run")
@limiter.limit("4/minute")
async def distill_run(request: Request):
    """Enqueue one distillation chain (setup → harvest → train → eval → install).

    The chain self-bootstraps its MLX toolchain on first use (phase ⓪), so this
    enqueues unconditionally — no terminal step. The engine re-checks every
    precondition (tooling, briefing count, disk) and writes any refusal into the
    status file. Training reads the local briefing archive — no cloud is involved.
    """
    result = await jobs.enqueue("distill", "manual")
    return {"status": result}


@router.get("/api/distill/tooling/install")
@limiter.limit("2/minute")
async def distill_tooling_install(request: Request):
    """EventSource: install the MLX toolchain, streaming progress to the card.

    Mirrors ``/api/model/download`` — a GET because the SPA streams progress over
    EventSource. The ~1 GB toolkit lands under the data dir, never the bundle, and
    the install is idempotent (already-present steps are skipped).
    """
    import json

    from estormi_distill.trainer import bootstrap_events, tooling

    async def stream():
        if tooling()["ready"]:
            yield {
                "data": json.dumps(
                    {"message": "✓ Toolchain already installed", "progress": 100, "status": "done"}
                )
            }
            return
        try:
            async for event in bootstrap_events():
                yield {"data": json.dumps(event)}
        except Exception:  # noqa: BLE001
            log.exception("distill.tooling.install.failed")
            yield {
                "data": json.dumps(
                    {"status": "error", "message": "Error: setup failed (see server logs)"}
                )
            }

    return EventSourceResponse(stream())


@router.post("/api/distill/tooling/delete")
@limiter.limit("4/minute")
async def distill_tooling_delete(request: Request):
    """Remove the installed MLX toolchain (venv + llama.cpp + base cache) to reclaim disk."""
    import shutil

    from estormi_distill.trainer import tools_dir

    tools = tools_dir()
    if tools.exists():
        shutil.rmtree(tools, ignore_errors=True)
    return {"removed": not tools.exists()}
