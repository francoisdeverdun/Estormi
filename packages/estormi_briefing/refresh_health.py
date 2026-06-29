"""Health-only briefing refresh — the wake-time fast path.

A full briefing run takes ~30 minutes locally; the morning flow therefore
composes it early (cron) with whatever health data exists, and when WHOOP
scores the night's recovery — minutes after the user wakes — this module
refreshes ONLY the readiness card:

1. pull fresh WHOOP data (one cloud API call, seconds);
2. load today's briefing from the vault — absent, degrade to a FULL run;
3. recompose the READINESS steer (one grammar-locked local call, ~30s,
   figures verified against the health rows — see ``composer``);
4. splice it between the renderer's readiness markers and push the JSON —
   the iOS companion is notified immediately with the fresh text;
5. re-narrate + re-synthesize the audio in the background, then push the
   JSON again SILENTLY (the audio file is replaced under the same path).

Total time-to-text at wake: ~1 minute. Spawned through the same launcher and
engine mutex as the full run (``ESTORMI_BRIEFING_REFRESH=health``), so it can
never race a running briefing.
"""

from __future__ import annotations

import asyncio
from datetime import datetime

import aiosqlite
import structlog

from estormi_briefing.compose.build_daily_note import splice_readiness_card
from estormi_briefing.compose.composer import _write_readiness
from estormi_briefing.compose.prompts import _vision_chunk_row
from estormi_briefing.day.day import LOCAL_TZ, _local_when_label
from estormi_briefing.day.day_context import _fetch_health_chunks
from estormi_briefing.io.delivery import _maybe_attach_audio
from estormi_briefing.lint.vision_lint import readiness_line_span
from estormi_briefing.llm import runtime
from estormi_briefing.llm.runtime import _get_setting, _set_setting
from estormi_ingestion.shared.delivery.vault_sync import push_briefing, read_briefing
from memory_core.timeparse import now_iso_z

log = structlog.get_logger()


async def _ingest_whoop() -> None:
    """Pull the latest WHOOP data (best-effort — the refresh proceeds with
    whatever health chunks exist if the pull fails)."""
    try:
        from estormi_ingestion.whoop.sync import sync as whoop_sync  # noqa: PLC0415

        stats = await asyncio.to_thread(whoop_sync)
        log.info("health refresh: whoop sync %s", stats)
    except Exception as exc:  # noqa: BLE001 — best-effort by design
        log.warning("health refresh: whoop sync failed (%r) — using stored data", exc)


async def _newest_health_ts(db: aiosqlite.Connection) -> str:
    """ISO timestamp of the newest stored WHOOP chunk (freshness bookkeeping)."""
    cur = await db.execute("SELECT COALESCE(MAX(date_ts), '') FROM chunks WHERE source = 'whoop'")
    row = await cur.fetchone()
    await cur.close()
    return str(row[0] or "")


async def _compose_run_summary(db: aiosqlite.Connection, note: str) -> str:
    """Fold a health-refresh note into the morning run's summary instead of
    replacing it. The morning full run writes the substantive
    ``knowledge_last_run_summary`` (sources/items/actions) that the status API
    surfaces; the wake-time refresh only touches readiness, so it appends its
    note rather than clobbering that summary. Falls back to the bare note when
    no prior summary exists."""
    prior = (await _get_setting(db, "knowledge_last_run_summary", "")).strip()
    return f"{prior} · {note}" if prior else note


async def run_refresh() -> str:
    """Refresh today's readiness card from fresh WHOOP data. Returns a summary.

    Falls back to the FULL briefing run when there is no briefing to refresh
    (the wake fired before the morning cron, or the day's run failed).
    """
    today = datetime.now(LOCAL_TZ).date()
    date_str = today.isoformat()

    await _ingest_whoop()

    briefing = read_briefing(date_str)
    if briefing is None or not briefing.get("htmlBody"):
        log.info("health refresh: no briefing for %s — running the full pipeline", date_str)
        from estormi_briefing.run_briefing import run  # noqa: PLC0415 — avoid import cycle

        return await run()

    from estormi_briefing.run_briefing import DB_PATH  # noqa: PLC0415 — avoid import cycle

    db = await aiosqlite.connect(DB_PATH)
    db.row_factory = aiosqlite.Row
    try:
        # Local by default — the provider/model picker was retired (briefings are
        # always-local two-quills, see run_briefing.run). Defaulting to a cloud
        # CLI here once silenced a whole morning's wake notification when that
        # call failed; keep this in lockstep with run_briefing's default.
        provider = await _get_setting(db, "knowledge_llm_provider", "local")
        model = await _get_setting(db, "knowledge_llm_model", "claude-sonnet-4-6")
        if provider == "local":
            from memory_core.llm_local import selected_tier_for  # noqa: PLC0415

            model = await selected_tier_for("briefing")
        lang_code = (await _get_setting(db, "briefing_language", "fr")).strip().lower()
        user_context = await _get_setting(db, "briefing_user_context", "")
        runtime.refresh(lang_code, user_context)

        health_chunks = await _fetch_health_chunks(today)
        for chunk in health_chunks:
            chunk["when_label"] = _local_when_label(chunk.get("date_ts"))
        health_rows = [r for c in health_chunks if (r := _vision_chunk_row(c))]
        if not health_rows:
            # No fresh health data to recompose the readiness card with — but the
            # morning briefing was composed SILENTLY (the WHOOP wake-trigger owns
            # delivery), so the wake must still ANNOUNCE it or the phone never
            # rings. Push the existing briefing unchanged with notify on — this
            # path INTENTIONALLY overrides ESTORMI_BRIEFING_NOTIFY: announcing is
            # the wake's whole purpose, so 'silent' must not suppress it here.
            summary = "health refresh: no health data — existing briefing announced"
            log.info(summary)
            push_briefing(briefing, notify=True)
            # The morning run's summary still describes what shipped — only
            # refresh the status, never the summary.
            await _set_setting(db, "knowledge_last_run_status", "ok")
            return summary

        readiness = await _write_readiness(
            lambda p, **kw: runtime._llm_call(p, provider, model, **kw),
            {"health_rows": health_rows},
            language=runtime.language,
        )
        span = readiness_line_span(readiness)
        if not span:
            # A failed/empty readiness recompose must NEVER swallow the wake
            # announce (a flaky cloud call once dropped the entire morning push):
            # ship the already-composed briefing unchanged so the phone still
            # rings, then record the partial outcome.
            summary = "health refresh: readiness refresh skipped — existing briefing announced"
            log.warning(
                "health refresh: readiness composition failed — announcing briefing unchanged"
            )
            push_briefing(briefing, notify=True)
            await _set_setting(db, "knowledge_last_run_status", "ok")
            await _set_setting(
                db,
                "knowledge_last_run_summary",
                await _compose_run_summary(db, "readiness refresh skipped; briefing announced"),
            )
            return summary
        steer = span[2].strip()

        new_body = splice_readiness_card(briefing["htmlBody"], steer, lang_code)
        if new_body is None:
            log.info("health refresh: no readiness card to splice — running the full pipeline")
            from estormi_briefing.run_briefing import run  # noqa: PLC0415

            return await run()

        briefing["htmlBody"] = new_body
        briefing["generatedAt"] = now_iso_z()
        briefing["healthDataAt"] = await _newest_health_ts(db)
        # Keep the structured field in lockstep with the rendered card. The SPA
        # field editor, the iOS reader and the distill trainer all consume
        # fields["readiness"]; without this the wake-time refresh updates only
        # htmlBody and the field keeps the (now stale) morning value — the exact
        # htmlBody≠fields divergence seen on 2026-06-20 and -22.
        briefing.setdefault("fields", {})["readiness"] = steer

        # Text first: the user gets the fresh steer on their phone within ~a
        # minute of waking; the (minutes-long) audio rework follows silently.
        push_briefing(briefing)
        log.info("health refresh: readiness updated and pushed (%d chars)", len(steer))

        await _maybe_attach_audio(db, date_str, new_body, briefing, provider, model)
        if briefing.get("audioPath"):
            push_briefing(briefing, notify=False)

        summary = "health refresh: readiness updated"
        await _set_setting(db, "knowledge_last_run_status", "ok")
        await _set_setting(
            db,
            "knowledge_last_run_summary",
            await _compose_run_summary(db, "readiness updated"),
        )
        return summary
    finally:
        await db.close()
