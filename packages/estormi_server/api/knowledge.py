"""Knowledge / Briefing REST API: status, run, stop, log tail, runs history.

Briefing generation is its own engine — ``run`` launches it (preempting the
other engines), ``stop`` kills it. ``status`` reports whether a briefing run
is in progress plus the last-run settings the briefing pipeline writes for
itself. ``runs`` exposes the structured per-run history the briefing engine
persists into ``briefing_runs``.
"""

from __future__ import annotations

import asyncio
import re

import structlog
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from estormi_server.server import jobs
from estormi_server.server.limiter import limiter
from estormi_server.services import knowledge as svc
from estormi_server.sql.connection import _get_setting
from estormi_server.storage.writers import delete_by_source_id

log = structlog.get_logger()

router = APIRouter()

# Re-export the JSON-column decoder under its historic name so the existing
# unit test (``from estormi_server.api.knowledge import _parse_json_col``)
# keeps resolving after the logic moved into ``services``.
_parse_json_col = svc.parse_json_col


@router.get("/api/knowledge/llm-models")
@limiter.limit("60/minute")
async def api_knowledge_llm_models(request: Request, provider: str = "local"):
    """Available models for the briefing LLM ``provider``, plus the settings
    key that stores the choice.

    The model picker is a dropdown whose options depend on the provider:

    - ``local``      → the GGUF tiers actually installed on disk. The local
      provider loads a tier (``briefing_model_tier``), not a free-text name.
    - ``claude-cli`` → the values the ``claude`` CLI's ``--model`` flag
      accepts: family aliases (``opus``/``sonnet`` resolve to the latest of
      each family) plus the full ``claude-fable-5`` ID (the CLI has no
      ``fable`` alias). The CLI has no list-models command, so these are its
      contract.
    - anything else  → no enumerable catalogue; the UI keeps the saved value.

    Returns ``{models: [{value, label}], current, setting_key}``.
    """
    provider = (provider or "").strip().lower()
    if provider == "local":
        from pathlib import Path  # noqa: PLC0415

        from memory_core.llm_local import (  # noqa: PLC0415
            MODEL_CATALOG,
            model_file_path,
            selected_tier_for,
        )

        models = [
            {"value": tier, "label": meta["label"]}
            for tier, meta in MODEL_CATALOG.items()
            if Path(model_file_path(tier)).exists()
        ]
        current = await selected_tier_for("briefing")
        return {"models": models, "current": current, "setting_key": "briefing_model_tier"}

    current = await _get_setting("knowledge_llm_model", "claude-sonnet-4-6")
    if provider == "claude-cli":
        # Fable + Opus + Sonnet — Haiku was dropped as a briefing-composition
        # choice after the model bench-off (it under-performed on the day-vision
        # and was the one Claude tier the critic rejected). Haiku is still used
        # internally as the cheap extractor/critic model — that's a separate
        # setting (briefing_extractor_model / briefing_critic_model), not a
        # composition choice the user picks here. Fable uses the full model ID
        # because the CLI rejects a bare ``fable`` alias.
        models = [
            {"value": "claude-fable-5", "label": "Fable 5"},
            {"value": "opus", "label": "Opus (latest)"},
            {"value": "sonnet", "label": "Sonnet (latest)"},
        ]
    else:  # unknown provider — no machine-readable model list to enumerate
        models = []
    return {"models": models, "current": current, "setting_key": "knowledge_llm_model"}


# Briefings are keyed by ISO date (YYYY-MM-DD). The strict pattern keeps the
# parameter from being abused as a path fragment when it is used to compose
# the vault filename.
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


@router.get("/api/knowledge/status")
@limiter.limit("60/minute")
async def api_knowledge_status(request: Request):
    return await svc.knowledge_status()


@router.get("/api/knowledge/log")
@limiter.limit("30/minute")
async def api_knowledge_log(request: Request, lines: int = 150):
    # Clamp the tail length so a caller can't ask for splitlines() to
    # materialise an unbounded list.
    lines = max(1, min(lines, 5000))
    try:
        if not jobs._KNOWLEDGE_LOG.exists():
            return {"content": "(log not found — pipeline has not run yet)"}
        from estormi_server.server.log_tail import tail_lines  # noqa: PLC0415

        tail = await asyncio.to_thread(tail_lines, str(jobs._KNOWLEDGE_LOG), lines)
        return {"content": tail}
    except Exception:
        log.exception("knowledge.log.read_failed")
        return JSONResponse({"error": "knowledge log read failed"}, status_code=500)


@router.post("/api/knowledge/run")
@limiter.limit("4/minute")
async def api_knowledge_run(request: Request):
    """Enqueue a briefing-generation run. The queue runner picks it up
    when the engine slot is free; idempotent by kind."""
    # A manual run is user-initiated: always announce it (notify="force"), even
    # when the WHOOP wake-trigger owns the silent morning pre-compute.
    result = await jobs.enqueue("briefing", "manual", payload={"notify": "force"})
    return {"status": result}


@router.post("/api/knowledge/refresh-health")
@limiter.limit("4/minute")
async def api_knowledge_refresh_health(request: Request):
    """Enqueue a health-only refresh of today's briefing (~1 minute): fresh
    WHOOP pull, readiness card recomposed and spliced, audio re-narrated in
    the background. Falls back to a full run when no briefing exists yet."""
    result = await jobs.enqueue("briefing", "manual", payload={"refresh": "health"})
    return {"status": result}


@router.get("/api/briefings")
@limiter.limit("60/minute")
async def api_list_briefings(request: Request):
    """List the assembled briefings on disk (newest first).

    The display path reads from the iCloud Drive vault, which stores each
    day's briefing as one self-contained JSON file with ``htmlBody``. The
    SQLite ``chunks`` table holds the same body split into overlapping
    windows for search — useful for retrieval, but lossy to reassemble.
    """
    from estormi_ingestion.shared.delivery.vault_sync import list_briefings  # noqa: PLC0415

    # iCloud Drive paths can stall on network sync; keep the glob off the loop.
    items = await asyncio.to_thread(list_briefings)
    return {"items": items}


@router.get("/api/briefings/{date}")
@limiter.limit("60/minute")
async def api_get_briefing(request: Request, date: str):
    """Return one day's assembled briefing JSON from the vault."""
    if not _DATE_RE.match(date):
        raise HTTPException(status_code=400, detail="date must be YYYY-MM-DD")
    from estormi_ingestion.shared.delivery.vault_sync import read_briefing  # noqa: PLC0415

    data = await asyncio.to_thread(read_briefing, date)
    if data is None:
        raise HTTPException(status_code=404, detail="briefing not found")
    return data


class BriefingFieldEdits(BaseModel):
    """Plain-text edits to one or more editable prose sections — the bare source
    the renderer re-wraps: the objective subtitle, the readiness steer (no
    label), and the My-day narrative prose. Omitted (``None``) sections are left
    untouched."""

    objective: str | None = Field(None, max_length=4_000)
    readiness: str | None = Field(None, max_length=4_000)
    myDay: str | None = Field(None, max_length=40_000)


class BriefingEditBody(BaseModel):
    # Two edit modes. ``htmlBody`` is the raw-HTML save (the legacy textarea, and
    # the fallback for briefings composed before section markers existed).
    # ``fields`` is the structured save: the server re-renders each named section
    # from plain text and splices it between that section's zone markers, so
    # Python keeps owning every tag and the derived timeline / Around / World
    # blocks are never touched. Exactly one of the two must be present.
    htmlBody: str | None = Field(None, min_length=1, max_length=200_000)
    fields: BriefingFieldEdits | None = None


def _apply_field_edits(existing: dict, edits: BriefingFieldEdits) -> tuple[str, dict]:
    """Re-render each provided section from plain text and splice it into the
    stored ``htmlBody``. Returns ``(new_html, merged_fields)``. Raises 422 when a
    named section has no zone markers (the briefing predates the field editor —
    the SPA should not have offered the form, but guard anyway)."""
    from estormi_briefing.compose.build_daily_note import splice_section  # noqa: PLC0415

    lang = (existing.get("lang") or "en").strip().lower() or "en"
    html = existing.get("htmlBody") or ""
    merged = dict(existing.get("fields") or {})
    for name, text in edits.model_dump(exclude_none=True).items():
        text = (text or "").strip()
        if not text:
            continue  # an emptied field is a no-op; clearing a section isn't supported
        spliced = splice_section(html, name, text, lang)
        if spliced is None:
            raise HTTPException(
                status_code=422,
                detail=f"section '{name}' has no edit markers in this briefing",
            )
        html = spliced
        merged[name] = text
    return html, merged


def _fold_edit_into_distill(date: str, html_body: str) -> None:
    """Best-effort: register a user-edited briefing as the highest-quality
    distillation reference and seed the exemplar bank with it. A failure here
    must never fail the save (the vault write already succeeded)."""
    try:
        from estormi_briefing.compose.exemplars import (  # noqa: PLC0415
            add_exemplars,
            harvest_exemplars,
        )
        from estormi_distill import references  # noqa: PLC0415

        references.register_edited_reference(date, html_body)
        for stage, texts in harvest_exemplars(html_body).items():
            add_exemplars(stage, texts, f"user-edited-{date}")
    except Exception:  # noqa: BLE001 — the learning hook is best-effort
        log.warning("briefing edit: distill fold-in failed", exc_info=True)


@router.put("/api/briefings/{date}")
@limiter.limit("20/minute")
async def api_edit_briefing(request: Request, date: str, body: BriefingEditBody):
    """Save a user-edited briefing back to the vault and fold the correction into
    the quill's training set.

    Accepts either ``fields`` (structured: the server re-renders each edited
    prose section and splices it between its zone markers, so the SPA never
    handles raw HTML) or ``htmlBody`` (the raw-HTML fallback for briefings
    composed before the markers existed). Either way a human-corrected briefing
    is the best possible distillation reference, so the save both updates the
    vault (the single source both the SPA and the iOS companion read) and
    registers the corrected text as a ``user-edited`` reference + exemplars for
    the next quill retrain. It also fires a best-effort "Briefing updated" APNs
    nudge (not the "new briefing" doorbell) so the edit surfaces on the iOS
    companion, which re-reads the vault on foreground."""
    if not _DATE_RE.match(date):
        raise HTTPException(status_code=400, detail="date must be YYYY-MM-DD")
    if body.fields is None and body.htmlBody is None:
        raise HTTPException(status_code=400, detail="provide htmlBody or fields")
    from estormi_ingestion.shared.delivery.vault_sync import (  # noqa: PLC0415
        notify_briefing_updated,
        push_briefing,
        read_briefing,
    )
    from memory_core.timeparse import now_iso_z  # noqa: PLC0415

    existing = await asyncio.to_thread(read_briefing, date)
    if existing is None:
        raise HTTPException(status_code=404, detail="briefing not found")
    if body.fields is not None:
        # Structured save: re-render the edited sections and splice them in,
        # leaving the rest of the body (and its markers) byte-for-byte intact.
        existing["htmlBody"], existing["fields"] = _apply_field_edits(existing, body.fields)
    else:
        existing["htmlBody"] = body.htmlBody
    existing["editedAt"] = now_iso_z()
    if not await asyncio.to_thread(push_briefing, existing, False):
        return JSONResponse({"error": "vault write failed"}, status_code=500)
    await asyncio.to_thread(_fold_edit_into_distill, date, existing["htmlBody"] or "")
    # Best-effort APNs nudge so the edit surfaces on the iOS companion (it
    # re-reads the vault on foreground / a 60s poll). Never blocks the save.
    await asyncio.to_thread(notify_briefing_updated, date)
    return {"date": date, "saved": True}


@router.delete("/api/briefings/{date}")
@limiter.limit("10/minute")
async def api_delete_briefing(request: Request, date: str):
    """Fully delete one day's briefing.

    Briefings are delivered to the vault only (they are not re-ingested as
    ``briefing``-source chunks), so the real effect here is removing the
    iCloud vault file the iOS companion reads. The ``delete_by_source_id``
    call below is a defensive cleanup of any legacy ``briefing-<date>``
    chunks left by an older build that did store them; today it is normally
    a no-op (``deleted: 0``).

    ``briefing_runs`` rows are intentionally left in place: they are an
    engine-performance log (durations, tokens, sections), not user content,
    and deleting them would skew the engine-history stats.
    """
    if not _DATE_RE.match(date):
        raise HTTPException(status_code=400, detail="date must be YYYY-MM-DD")
    result = await delete_by_source_id("briefing", f"briefing-{date}")
    # Always attempt the vault delete — even when no chunks were found,
    # the iOS-side file may still exist (e.g. chunks were wiped earlier).
    # Delete is idempotent: a missing briefing returns ``deleted: 0`` rather
    # than 404 so the UI doesn't error when the user clicks twice.
    from estormi_ingestion.shared.delivery.vault_sync import delete_briefing  # noqa: PLC0415

    vault_ok = await asyncio.to_thread(delete_briefing, date)
    return {"deleted": result.get("deleted", 0), "date": date, "vault": vault_ok}


@router.post("/api/briefings/reset")
@limiter.limit("3/minute")
async def api_briefings_reset(request: Request):
    """Wipe every composed briefing plus the engine's run history.

    Removes:
      * any legacy chunks with ``source = 'briefing'`` (and their vectors) —
        current briefings are vault-only, so this is normally a no-op kept to
        clean up data from an older build that did store them,
      * every ``briefing_runs`` row and every ``dag_runs`` row scoped to
        ``engine = 'briefing'`` — these feed the engine-history strip,
      * the briefing files under ``<vault>/briefings/`` and the manifest
        listing them,
      * the rolling ``knowledge.log`` file.

    Settings (cron, last-run state, knowledge_enabled, sources YAML) are
    kept. Nothing relaunches — the next scheduled run rebuilds from scratch.
    """
    from memory_core.audit import log_security_decision  # noqa: PLC0415

    log_security_decision(
        decision="accept",
        path="/api/briefings/reset",
        client_host=request.client.host if request.client else "",
        reason="briefings_reset",
        method="POST",
    )
    return await svc.reset_briefings()


@router.post("/api/knowledge/stop")
@limiter.limit("10/minute")
async def api_knowledge_stop(request: Request):
    return await svc.stop_briefing()


@router.get("/api/knowledge/runs")
@limiter.limit("60/minute")
async def api_knowledge_runs(request: Request, days: int = 14, limit: int = 50):
    """Recent briefing_runs rows, newest-first.

    Powers the BriefingPulse stat tiles (most recent latency / tokens /
    coverage) and the table of past runs on the Briefing page. Restricted
    to the last ``days`` days so the response stays small on long-running
    installs; ``limit`` caps the row count regardless of the time window.
    """
    days = max(1, min(days, 365))
    limit = max(1, min(limit, 500))
    return {"runs": await svc.recent_runs(days, limit)}
