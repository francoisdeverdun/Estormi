"""Ingestion pipeline control endpoints.

Covers:
  - ``GET  /api/pipeline``                — full snapshot for the dashboard
  - ``POST /api/pipeline/run``            — kick off the daily pipeline manually
  - ``POST /api/pipeline/stop``           — terminate a running pipeline (by PGID)
  - ``GET  /api/pipeline/stage-log``      — tail of a stage log (allow-listed paths)
  - ``GET  /api/timeseries``              — per-source cumulative store / daily ingestion
  - ``PUT  /api/sources/{name}/watermark/reset``

Process state lives in ``server.jobs``; this module only reads from it.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import structlog
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from estormi_server.server import jobs
from estormi_server.server.limiter import limiter
from estormi_server.server.sources import is_valid_source_slug
from estormi_server.services.pipeline_status import get_pipeline_data
from estormi_server.storage.tools import DATA_DIR, sqlite_conn, write_txn

log = structlog.get_logger()

router = APIRouter()


@router.get("/api/pipeline")
@limiter.limit("120/minute")
async def api_pipeline(request: Request):
    # `get_pipeline_data` shells out to `pgrep` and opens a fresh sqlite
    # connection inside `_chunks_added_since`; both block the event loop
    # at the dashboard's poll cadence. Push the whole thing to a thread.
    return await asyncio.to_thread(get_pipeline_data)


@router.post("/api/pipeline/run")
@limiter.limit("4/minute")
async def api_pipeline_run(request: Request, stage: str | None = None):
    """Enqueue a pipeline run. Optionally scoped to a single stage.

    ``?stage=notes`` runs ONLY that stage (still respects per-source
    ``source_<key>_enabled``; the validation happens inside ``_run_dag``).
    This is the per-source ▶ play button on the Procession of Sources
    panel. The call returns as soon as the run is queued — the queue
    runner in ``server.jobs`` picks it up when the engine slot is free.
    """
    stage_override: str | None = None
    if stage is not None:
        stage = stage.strip()
        if not is_valid_source_slug(stage):
            return JSONResponse({"error": f"invalid stage: {stage!r}"}, status_code=400)
        stage_override = stage
    payload = {"stage_override": stage_override} if stage_override else None
    result = await jobs.enqueue("ingestion", "manual", payload=payload)
    # Surface duplicate-launch refusals as 409 so the SPA catches them
    # through `apiSend`'s !r.ok throw and shows them in the error banner.
    # A silent 200 looked like the button did nothing.
    if result in ("already_running", "already_queued"):
        return JSONResponse(
            {"status": result, "stage": stage_override},
            status_code=409,
        )
    return {"status": result, "stage": stage_override}


@router.post("/api/pipeline/stop")
@limiter.limit("4/minute")
async def api_pipeline_stop(request: Request):
    # Drop any waiting ingestion entry first so the queue runner doesn't
    # immediately re-dispatch a fresh pipeline run after this kill — same race the
    # other engines' stop endpoints handle.
    await jobs.remove_from_queue("ingestion")
    # Is there anything to stop? Prefer the in-memory handles, fall back to the
    # engine lock (survives a server restart). If nothing owns the slot, 409.
    pgid = jobs._dag_pgid
    if pgid is None and jobs._dag_proc and jobs._dag_proc.returncode is None:
        pgid = jobs._dag_proc.pid
    if pgid is None:
        pgid = await jobs._locked_pgid("ingestion")
    if pgid is None:
        return JSONResponse({"status": "not_running"}, status_code=409)
    # Reuse the launcher's kill path: SIGTERM → SIGKILL escalation + lock release.
    await jobs._kill_dag_processes()
    return {"status": "stopped"}


_ENGINE_LOG_NAMES = {
    "ingestion": "estormi-daily-dag.log",
}


def _aggregate_source_run_logs(source: str, logs_dir: str, lines: int) -> str:
    """Aggregate every log a source has produced into one timeline.

    A source writes through two paths, and the live-tail modal wants a single
    continuous view across both:

      * ``source-<name>.log`` — one cumulative file appended to by the
        manual-run endpoint (``api/sources_admin.py``);
      * ``estormi-stage-<RUNID>-<name>.log`` — one file per pipeline run, written
        by ``scripts/daily_ingestion.sh``.

    Earlier this function only read the per-run files, and the endpoint
    short-circuited to the cumulative file whenever it existed — so once a
    source had been run manually, the modal froze on that stale cumulative
    file and never showed later cron runs. Merge both sets instead, ordered
    by file mtime (the two naming schemes don't interleave lexically, but
    mtime is chronological for either), insert a ``── run X ──`` marker
    between files, then tail the last ``lines`` of the concatenation so the
    newest activity always lands at the end.

    Read all files in memory because each is small (the per-run logs are
    capped by ``scripts/daily_ingestion.sh``); the final tail still slices to
    the requested line count so the response stays bounded.
    """
    import glob  # noqa: PLC0415
    import os  # noqa: PLC0415

    files = glob.glob(os.path.join(logs_dir, f"estormi-stage-*-{source}.log"))
    cumulative = os.path.join(logs_dir, f"source-{source}.log")
    if os.path.exists(cumulative):
        files.append(cumulative)
    if not files:
        return ""
    # Order by mtime so the most recent run is last regardless of which path
    # wrote it; a missing file (raced deletion) sorts to the front harmlessly.
    files.sort(key=lambda fp: os.path.getmtime(fp) if os.path.exists(fp) else 0.0)
    chunks: list[str] = []
    for fp in files:
        base = os.path.basename(fp)
        if base.startswith("estormi-stage-"):
            # Run id is the timestamp segment between "estormi-stage-" and the
            # trailing "-<source>.log" — used as the section header.
            run_id = base[len("estormi-stage-") : -len(f"-{source}.log")]
        else:
            run_id = "manual runs"
        try:
            with open(fp, encoding="utf-8", errors="replace") as fh:
                body = fh.read().rstrip()
        except OSError:
            continue
        if not body:
            continue
        chunks.append(f"── run {run_id} ──\n{body}")
    if not chunks:
        return ""
    combined = "\n\n".join(chunks)
    # Tail to the requested line count from the END (newest run is last in
    # ``files``, so the last ``lines`` covers the most recent activity).
    all_lines = combined.split("\n")
    return "\n".join(all_lines[-lines:])


@router.get("/api/pipeline/stage-log")
@limiter.limit("30/minute")
async def api_pipeline_stage_log(
    request: Request,
    path: str | None = None,
    engine: str | None = None,
    source: str | None = None,
    lines: int = 2000,
):
    """Return the tail of a stage log file.

    One of ``path`` (allow-listed), ``engine`` (logical engine name) or
    ``source`` (per-source ingestion log) must be provided. ``engine``
    resolves to the canonical ``$DATA_DIR/logs/`` file for that engine;
    ``source=<name>`` resolves to ``$DATA_DIR/logs/source-<name>.log``,
    so the SPA doesn't need to know server-side paths.

    Allow-list of acceptable locations:
      * ``$DATA_DIR/logs/`` (production stage logs are written here by
        ``scripts/daily_ingestion.sh``);
      * ``/tmp`` (and its ``/private/tmp`` twin on macOS) **only** when the
        filename starts with ``estormi-`` — keeps legacy / fallback log
        paths working without doubling as a generic /tmp file reader.
    """
    try:
        if source is not None:
            # Source names are slugs (a-z, 0-9, _, -). Anything else is rejected
            # before we touch the filesystem.
            if not is_valid_source_slug(source):
                return JSONResponse({"error": f"invalid source name: {source!r}"}, status_code=400)
            # A source writes through two log paths — ``source-<name>.log``
            # (cumulative, manual-run endpoint) and one
            # ``estormi-stage-<RUNID>-<name>.log`` per pipeline run (the cron). The
            # live tail aggregates *both* into one chronological timeline;
            # short-circuiting to the cumulative file used to freeze the modal
            # on a stale manual run once later cron runs landed.
            logs_dir = Path(DATA_DIR) / "logs"
            lines = max(1, min(lines, 5000))
            text = await asyncio.to_thread(_aggregate_source_run_logs, source, str(logs_dir), lines)
            return {"path": f"source:{source}", "content": text}
        elif engine is not None:
            log_name = _ENGINE_LOG_NAMES.get(engine)
            if not log_name:
                return JSONResponse({"error": f"unknown engine: {engine}"}, status_code=400)
            path = str(Path(DATA_DIR) / "logs" / log_name)
        elif not path:
            return JSONResponse({"error": "path, engine or source required"}, status_code=400)

        log_path = Path(path).resolve()
        path_str = str(log_path)

        data_logs = str((Path(DATA_DIR) / "logs").resolve())
        in_data_logs = path_str.startswith(data_logs + "/")

        tmp_resolved = str(Path("/tmp").resolve())
        in_tmp = path_str.startswith(tmp_resolved + "/")
        name_ok = log_path.name.startswith("estormi-")

        if not (in_data_logs or (in_tmp and name_ok)):
            return JSONResponse({"error": "path not allowed"}, status_code=403)
        if not log_path.exists():
            return JSONResponse({"error": "log not found"}, status_code=404)
        lines = max(1, min(lines, 5000))
        from estormi_server.server.log_tail import tail_lines  # noqa: PLC0415

        text = await asyncio.to_thread(tail_lines, str(log_path), lines)
        return {"path": str(log_path), "content": text}
    except Exception:
        log.exception("pipeline.stage_log.error")
        return JSONResponse({"error": "stage-log read failed"}, status_code=500)


@router.get("/api/timeseries")
@limiter.limit("120/minute")
async def api_timeseries(request: Request, days: int = 14, mode: str = "memory"):
    """Per-source stacked-area data for the macOS ``IngestionPulse`` chart.

    Two views over the same window, selected by ``mode``:

      * ``ingestion`` — chunks *added* per day per source (a spiky daily
        delta);
      * ``memory`` (default) — the cumulative *store* per source, so the
        stack climbs monotonically and the last day lands on the all-time
        total. This mirrors the iOS companion's "Memoria" card and the
        ``memory`` block of ``jobs._build_vault_metrics``.

    Buckets ``chunks.ingested_at`` (UTC ``YYYY-MM-DD HH:MM:SS``) by calendar
    day and source; the response shape (``days`` / ``sources`` /
    ``series[].by_source``) is the contract the dashboard chart follows.

    This route was lost when the Extraction/Correlation engines were deleted
    (it lived in the removed ``api/entities.py``), which silently blanked the
    dashboard graph. Re-homed here since it reads only the ingestion ``chunks``
    table — no entity machinery.
    """
    days = max(1, min(days, 365))
    db = sqlite_conn()
    # Delta + cumulative-baseline shaping is shared with the iOS vault-metrics
    # snapshot — single source of truth in ``server.vault_metrics`` so the two
    # charts can't drift.
    from estormi_server.server.vault_metrics import compute_chunk_timeseries  # noqa: PLC0415

    return await compute_chunk_timeseries(db, days, mode)


@router.put("/api/sources/{name}/watermark/reset")
@limiter.limit("30/minute")
async def reset_watermark(request: Request, name: str):
    # Source names are slugs (alnum, _, -) — validate before any DB access,
    # matching reset_source_data / api_pipeline_run / stage-log.
    if not is_valid_source_slug(name):
        return JSONResponse({"error": f"invalid source: {name!r}"}, status_code=400)
    # WhatsApp's progress watermark is the durable-log key `whatsapp_log`, not the
    # `whatsapp` chunk slug; target it so the reset actually re-derives the log.
    wm_source = "whatsapp_log" if name == "whatsapp" else name
    # Serialised and rollback-guarded — the identical DELETE in admin.py
    # goes through the same span. Leaf, not re-entrant. See ``tools.write_txn``.
    async with write_txn() as db:
        await db.execute("DELETE FROM ingestion_watermarks WHERE source = ?", (wm_source,))
    return {"status": "ok", "source": name, "message": "Next run will do full import"}
