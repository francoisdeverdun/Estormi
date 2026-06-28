"""Settings-page overview aggregator.

One JSON snapshot of everything the Settings UI shows on load: settings,
storage sizes, source counts, model status, MCP token, pipeline summary,
WhatsApp sidecar, governor decision. The SPA fetches this once on load
and renders every section from it; subsequent edits go through the
specialised endpoints.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import httpx
import structlog
from fastapi import APIRouter, Request

from estormi_server.api.knowledge_sources import _kb_yaml_load
from estormi_server.integrations.whatsapp_sidecar import SIDECAR_URL, sidecar_headers
from estormi_server.server.limiter import limiter
from estormi_server.services import overview as svc

log = structlog.get_logger()

router = APIRouter()

# Re-export the two pure helpers (now in ``services.overview``) under their
# historic names so ``tests/estormi_server/test_settings_page_full.py`` keeps
# resolving to the same callables.
_fmt_bytes = svc.fmt_bytes
_dir_size = svc.dir_size


@router.get("/api/settings/overview")
@limiter.limit("30/minute")
async def settings_overview(request: Request):
    """One-shot JSON snapshot of everything the Settings UI shows on load."""
    from estormi_server.storage.tools import DATA_DIR, sqlite_conn  # noqa: PLC0415
    from memory_core.llm_local import _model_path, is_loaded  # noqa: PLC0415

    db = sqlite_conn()
    cursor = await db.execute("SELECT key, value FROM settings")
    settings_map: dict[str, str] = {row[0]: row[1] for row in await cursor.fetchall()}
    await cursor.close()

    data_dir = Path(DATA_DIR)
    db_path = data_dir / "estormi.db"
    qdrant_path = data_dir / "qdrant"
    # Match where the ingestion scripts actually write (see
    # estormi_ingestion/imessage/fetch_imessages.py and
    # estormi_ingestion/whatsapp/ingest_conversations.py): $STAGING_DIR, default
    # ~/estormi-staging. Not under DATA_DIR.
    staging_path = Path(os.environ.get("STAGING_DIR") or (Path.home() / "estormi-staging"))

    db_size, qdrant_size, staging_size = await asyncio.gather(
        asyncio.to_thread(lambda: db_path.stat().st_size if db_path.exists() else 0),
        svc.cached_dir_size(qdrant_path),
        svc.cached_dir_size(staging_path),
    )

    cursor = await db.execute("SELECT source, COUNT(*) as n FROM chunks GROUP BY source")
    source_counts: dict[str, int] = {row[0]: row[1] for row in await cursor.fetchall()}
    total_chunks = sum(source_counts.values())

    cursor = await db.execute("SELECT source, last_fetched_at FROM ingestion_watermarks")
    watermarks: dict[str, str] = {row[0]: row[1] for row in await cursor.fetchall()}
    # WhatsApp now tracks progress with a real timestamp watermark, but it is
    # stored under the durable-log key `whatsapp_log` (the `whatsapp` source slug
    # carries the chunks). Surface it under `whatsapp` so the source row shows the
    # last-ingested time instead of the legacy "live staging" placeholder.
    if "whatsapp" not in watermarks and watermarks.get("whatsapp_log"):
        watermarks["whatsapp"] = watermarks["whatsapp_log"]

    # WhatsApp durable message log ("cache") — its own slice of the SQLite file,
    # broken out of db_bytes in the storage bar. dbstat gives accurate page bytes
    # for the table + its indexes; fall back to a content-length estimate if the
    # build lacks the dbstat vtab.
    whatsapp_cache_bytes = 0
    try:
        cur = await db.execute(
            "SELECT COALESCE(SUM(pgsize), 0) FROM dbstat WHERE name LIKE 'whatsapp_messages%'"
        )
        row = await cur.fetchone()
        await cur.close()
        whatsapp_cache_bytes = int(row[0]) if row and row[0] else 0
    except Exception:
        try:
            cur = await db.execute(
                "SELECT COALESCE(SUM(LENGTH(text) + LENGTH(msg_id) + LENGTH(chat_id) + "
                "LENGTH(COALESCE(chat_name, '')) + LENGTH(COALESCE(sender_name, '')) + "
                "LENGTH(ts_iso)), 0) FROM whatsapp_messages"
            )
            row = await cur.fetchone()
            await cur.close()
            whatsapp_cache_bytes = int(row[0]) if row and row[0] else 0
        except Exception:
            whatsapp_cache_bytes = 0

    try:
        model_path = Path(await _model_path())
        model_loaded = await is_loaded()
        model_exists = model_path.exists()
        model_size = model_path.stat().st_size if model_exists else 0
        from estormi_server.api.model import _infer_tier  # noqa: PLC0415

        tier = _infer_tier(model_path.name)
        model_name = model_path.name
    except Exception:
        # best-effort: model probe failed, report unloaded with safe defaults
        model_loaded = False
        model_exists = False
        model_size = 0
        tier = settings_map.get("briefing_model_tier", "ministral3-14b")
        model_name = ""

    # Pipeline next/last run (best-effort)
    next_run_at = last_run_started = ""
    overall_status = "unknown"
    last_run_failed_stages: list[str] = []
    try:
        from estormi_server.services.pipeline_status import (
            get_pipeline_data as _gpd,  # noqa: PLC0415
        )

        # _gpd() shells out to pgrep and opens a sync sqlite connection — wrap
        # so a slow disk can't stall the event loop during the Settings poll.
        _pd = await asyncio.to_thread(_gpd)
        next_run_at = _pd.get("next_run_at", "") or ""
        last_run_started = _pd.get("last_run_started", "") or ""
        overall_status = _pd.get("overall_status", "unknown") or "unknown"
        # SourcesPanel uses this to flip per-source chips to "Error" without
        # tarring every source with the global overall_status.
        last_run_failed_stages = list(_pd.get("last_run_failed_stages") or [])
    except Exception:
        log.debug("overview.pipeline_summary_failed", exc_info=True)

    # WhatsApp sidecar status (short timeout, never blocks the page)
    try:
        async with httpx.AsyncClient(timeout=0.5) as _wac:
            _war = await _wac.get(f"{SIDECAR_URL}/api/whatsapp/status", headers=sidecar_headers())
            wa = _war.json()
    except Exception:
        # best-effort: sidecar unreachable, report WhatsApp as unavailable
        wa = {"connected": False, "paired": False, "session_state": "UNAVAILABLE"}

    # iMessage Full Disk Access. The Tauri shell probes chat.db at launch
    # (a real open + read, not a stat) and writes the result here — the
    # sandboxed Python sidecar cannot read FDA-protected paths itself.
    #   "1"      → granted (True)
    #   "0"      → denied  (False) → the UI surfaces the FDA onboarding
    #   "absent" → no chat.db at all → FDA is moot, report None (no problem)
    # None = flag absent/unreadable, or "absent": running from source without
    # the native shell, before first launch, or no Messages history.
    imessage_fda: bool | None
    try:
        _fda_flag = (data_dir / "imessage-fda.flag").read_text(encoding="utf-8").strip()
        imessage_fda = True if _fda_flag == "1" else None if _fda_flag == "absent" else False
    except OSError:
        imessage_fda = None

    # Governor — the local-LLM sizing the resource governor settled on for the
    # briefing engine. Read-only on the page: the engine tunes itself to the
    # machine, the Settings board only reports the decision. When the model is
    # resident we show the rung it loaded with; otherwise the rung the governor
    # would pick right now (the start rung sized from the machine's RAM).
    governor: dict = {}
    try:
        from memory_core import resource_guard  # noqa: PLC0415
        from memory_core.llm_local import _LLM_LADDER, _start_rung, loaded_config  # noqa: PLC0415

        cfg = loaded_config()
        model_resident = cfg is not None
        if cfg is None:
            cfg = _LLM_LADDER[_start_rung()]
        n_ctx = int(cfg.get("n_ctx", 0))

        governor = {
            "context_window": n_ctx,
            "n_gpu_layers": int(cfg.get("n_gpu_layers", 0)),
            "memory_pressure": resource_guard.memory_pressure(),
            "model_resident": model_resident,
        }
    except Exception:
        # best-effort: the governor readout is page metadata, never required
        governor = {}

    version, knowledge_sources = await asyncio.gather(
        asyncio.to_thread(svc.read_version),
        asyncio.to_thread(_kb_yaml_load),
    )

    return {
        "version": version,
        "data_dir": str(data_dir),
        "settings": settings_map,
        "storage": {
            "db_bytes": db_size,
            "qdrant_bytes": qdrant_size,
            "staging_bytes": staging_size,
            "whatsapp_cache_bytes": whatsapp_cache_bytes,
            "total_chunks": total_chunks,
        },
        "sources": {
            "counts": source_counts,
            "watermarks": watermarks,
        },
        "model": {
            "name": model_name,
            "loaded": model_loaded,
            "exists": model_exists,
            "size_bytes": model_size,
            "tier": tier,
        },
        # The MCP bearer token is deliberately NOT surfaced here. The Settings
        # snapshot is served to any loopback caller without auth, and the SPA
        # never consumes the token — echoing it back would leak the keychain
        # secret to any local process that can reach the port.
        "mcp": {
            "port": settings_map.get("mcp_port", "8000"),
            "bind_address": settings_map.get("mcp_bind_address", "127.0.0.1"),
        },
        "pipeline": {
            "next_run_at": next_run_at,
            "last_run_started": last_run_started,
            "overall_status": overall_status,
            "last_run_failed_stages": last_run_failed_stages,
        },
        "whatsapp": wa,
        "knowledge_sources": knowledge_sources,
        "permissions": {"imessage_fda": imessage_fda},
        "governor": governor,
    }
