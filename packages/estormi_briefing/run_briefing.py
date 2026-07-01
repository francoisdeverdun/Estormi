"""Entrypoint: fetch → LLM → build → deliver.

Can be run standalone:
    python -m estormi_briefing.run_briefing

Or triggered via POST /api/knowledge/run from the MCP server.

This module is the thin **orchestrator** for the Briefing engine: it owns
``run()`` (the pipeline loop) and the per-run settings/config/status plumbing.
The actual work lives in focused siblings — ``mcp_io`` (MCP HTTP reads),
``world_corpus`` (world-corpus read + reassembly), ``day_context`` (per-day
personal-corpus fetches + actions), ``synthesis`` (LLM editorial passes),
``day_vision`` (the unifying cross-source pass), ``delivery`` (narration +
audio), plus ``day`` / ``prompts`` / ``llm_dispatch`` and the shared run-scoped
state in ``runtime``.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import time
from datetime import date, datetime
from pathlib import Path

import aiosqlite
import structlog
import yaml

from estormi_briefing.compose.build_daily_note import (
    _t,
    briefing_fields,
    briefing_title,
    build_note,
)
from estormi_briefing.compose.prompts import (
    CRITIC_JSON_SCHEMA,
    FACT_CRITIC_JSON_SCHEMA,
    _extract_topics_from_items,
    _personal_context_block,
    critique_briefing,
    fact_critique_briefing,
    format_critic_feedback,
)
from estormi_briefing.compose.synthesis import (
    _SOURCE_CONCURRENCY,
    _consolidate_items,
    _summarize_world_source,
    _synthesize_news,
    _synthesize_themes,
)
from estormi_briefing.compose.timeline import free_slots, timeline_html
from estormi_briefing.compose.user_profile import merge_profile, propose_observations, split_profile
from estormi_briefing.day.day import LOCAL_TZ
from estormi_briefing.day.day_context import _fetch_daily_actions
from estormi_briefing.day.day_vision import (
    _condense_readiness_line,
    _generate_day_vision,
    _repair_voice,
)
from estormi_briefing.io.delivery import _collapse_summary, _maybe_attach_audio
from estormi_briefing.io.mcp_io import _fetch_around_mcp
from estormi_briefing.io.world_corpus import (
    _fetch_world_followup,
    _fetch_world_today,
    _group_world_items,
)
from estormi_briefing.lint.fact_lint import allowed_date_set, lint_dates, lint_weekdays
from estormi_briefing.lint.vision_lint import lint_vision, objective_body_divergence
from estormi_briefing.llm import runtime
from estormi_briefing.llm.bestof import TimeBudget
from estormi_briefing.llm.llm_dispatch import _HAIKU_MODEL
from estormi_briefing.llm.metrics import _BriefingMetrics
from estormi_briefing.llm.runtime import _get_setting, _set_setting

# The briefing engine no longer fetches transcripts/articles itself — the
# `knowledge` pipeline stage (ingest_world.py) ingests them as corpus=world, and the
# briefing reads them back from the DB. ``source_key`` maps a stored world
# chunk's ``source_id`` prefix back to its config entry.
from estormi_ingestion.knowledge.ingest_world import source_key, validate_sources
from estormi_ingestion.shared.delivery.vault_sync import list_briefings as _vault_list_briefings
from estormi_ingestion.shared.delivery.vault_sync import push_briefing as _vault_push_briefing
from estormi_ingestion.shared.paths import estormi_data_dir, estormi_db_path
from memory_core.sanitizer import sanitize_chunk
from memory_core.timeparse import now_iso_z

DATA_DIR = str(estormi_data_dir())
DB_PATH = estormi_db_path()

PIPELINE_BUDGET_S = float(os.getenv("BRIEFING_WALL_CLOCK_BUDGET_S", "0"))

# High-confidence fact-critic verdicts: a draft that inverts a relation, flips a
# status, or misattributes a fact says something the data contradicts. These —
# and ONLY these — gate the degrade-soft overwrite guard below (C1). Style,
# structure lint, advisory coherence and ``critic_unavailable`` never gate: they
# are best-effort quality nudges, not falsehoods, so they must never block a
# ship. ``unsupported_claim`` is deliberately excluded — it is the softest
# fact-critic type (a claim merely unbacked, not provably wrong).
_SEVERE_ISSUE_TYPES = frozenset(
    {"status_polarity_inverted", "relation_inverted", "fact_misattributed"}
)

# When launched by the MCP server, jobs.py redirects this process's stdout
# and stderr into the briefing log file, so a FileHandler here would double
# every line. Stream to stderr only; the parent captures it. Standalone runs
# see the same output in the terminal.
logging.basicConfig(
    level=logging.INFO,
    format="[briefing] %(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
    handlers=[logging.StreamHandler(sys.stderr)],
)
structlog.configure(
    processors=[
        structlog.stdlib.add_log_level,
        structlog.stdlib.PositionalArgumentsFormatter(),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
    ],
    logger_factory=structlog.stdlib.LoggerFactory(),
    wrapper_class=structlog.stdlib.BoundLogger,
    cache_logger_on_first_use=True,
)
log = structlog.get_logger()


# ── Config validation ─────────────────────────────────────────────────────────


def load_sources(config_path: Path) -> list[dict]:
    # ``strict=True``: a misconfigured roster must fail the briefing run loudly.
    # Schema + defaults live in ``ingest_world.validate_sources`` (shared with the
    # lenient ``knowledge`` ingestion stage) so the two cannot drift apart.
    with open(config_path, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return validate_sources(data.get("sources", []) or [], strict=True)


# ── Main pipeline ─────────────────────────────────────────────────────────────


async def _resolve_run_config(
    db: aiosqlite.Connection, config_path: Path | None, explicit_config: bool
):
    """Resolve the per-run provider/model/language settings, start the metric
    accumulator, and pick the effective sources config.

    Sets ``runtime._run_metrics`` as a side effect (it must exist before any
    later ``runtime._llm_call`` records into it, and so the ``finally`` block
    persists it even when a subsequent phase raises). Returns the resolved
    ``(provider, model, model_label, lang_code, config_path)``; without an
    explicit path the config is the data-dir YAML the Briefing page edits (or
    the ``knowledge_sources_yaml`` setting override), and ``run()`` reports it
    missing when neither exists.
    """
    # The briefing reasons through the two bundled local quills by default; the
    # provider/model picker was retired from the UI. ``knowledge_llm_provider``
    # is still honoured if explicitly set (and the env override below) so an
    # ad-hoc cloud re-compose stays possible, but a stock install runs local.
    provider = await _get_setting(db, "knowledge_llm_provider", "local")
    model = await _get_setting(db, "knowledge_llm_model", "claude-sonnet-4-6")
    # Per-run provider/model overrides (paired with ESTORMI_BRIEFING_DATE) for
    # re-composing a specific past day by hand without touching the user's live
    # selection — see _resolve_today.
    env_provider = os.getenv("ESTORMI_BRIEFING_PROVIDER", "").strip()
    env_model = os.getenv("ESTORMI_BRIEFING_MODEL", "").strip()
    if env_provider:
        provider = env_provider
    if env_model:
        model = env_model
    # The cloud provider sends the composed prompt — which contains the day's
    # personal memory — off the Mac to Anthropic. It is off by default and not
    # exposed in the UI, so reaching it means someone set the setting/env
    # deliberately. Make that flow auditable: never let a cloud-composed run be
    # silent. (README + SECURITY.md disclose this; this is the per-run trail.)
    if provider != "local":
        log.warning(
            "briefing.cloud_provider_active: composing via cloud provider %r — "
            "the briefing prompt (your personal memory for the day) leaves this Mac. "
            "Set knowledge_llm_provider back to 'local' to keep everything on-device.",
            provider,
        )
    # For the local provider the model that actually composes the briefing is
    # the selected GGUF tier, NOT knowledge_llm_model (that key only feeds the
    # claude-cli provider's --model flag). Resolve it so the log line, the
    # briefing_runs row and the footer all name the real model.
    if provider == "local":
        from memory_core.llm_local import selected_tier_for  # noqa: PLC0415

        model = await selected_tier_for("briefing")
    # Per-stage tier routing (two-quills mode) — run-scoped, local-only; reset
    # explicitly for cloud providers so a stale map never leaks into a run.
    from estormi_briefing.llm.decode_profiles import set_stage_routing  # noqa: PLC0415

    # Both bundled quills are always in play on the local provider: the
    # two-quills preset routes each stage to the model that wins it (and the
    # critics to the other family). Default to the preset when no explicit
    # spec is stored, so a stock install uses both LLMs without a setting.
    routing_spec = (
        ((await _get_setting(db, "briefing_stage_routing", "")).strip() or "two-quills")
        if provider == "local"
        else ""
    )
    routing = set_stage_routing(routing_spec)
    model_label = f"{provider}/{model}" if provider and model else (model or provider)
    if routing:
        model_label = f"{provider}/two-quills({model})"
    # Refresh run-scoped state (output language, user display names, and the
    # user's profile) from the current settings/env so a Manage-modal flip
    # takes effect on the next run without restarting the server.
    # French-only edition: French is the default (there is no language selector).
    lang_code = (await _get_setting(db, "briefing_language", "fr")).strip().lower()
    user_context = await _get_setting(db, "briefing_user_context", "")
    runtime.refresh(lang_code, user_context)
    # Start the per-run metric accumulator. From here on every `_llm_call`
    # adds its prompt/output chars to it, and section / item counts are
    # filled in at the right hook points below. Persisted in the finally
    # block so even a failed run leaves a `briefing_runs` row.
    runtime._run_metrics = _BriefingMetrics(model=model, provider=provider)

    if not explicit_config:
        data_dir_yaml = Path(DATA_DIR) / "knowledge_sources.yaml"
        config_override = await _get_setting(db, "knowledge_sources_yaml", "")
        if data_dir_yaml.exists():
            config_path = data_dir_yaml
            await _put_setting(db, "knowledge_sources_yaml", str(data_dir_yaml))
        elif config_override and Path(config_override).exists():
            config_path = Path(config_override)
        else:
            # Canonical location (still absent) — run() reports it missing.
            config_path = data_dir_yaml

    log.info("Provider: %s, model: %s, config: %s", provider, model_label, config_path)
    return provider, model, model_label, lang_code, config_path


async def _put_setting(db: aiosqlite.Connection, key: str, value: str) -> None:
    """Persist a run-state setting (run status, topics, critic verdicts,
    profile stamps). Every settings write in this module funnels through here."""
    await _set_setting(db, key, value)


def _resolve_today() -> tuple[date, str]:
    """Resolve the briefing's target day.

    Defaults to "now", but ESTORMI_BRIEFING_DATE (YYYY-MM-DD) overrides it so a
    past day can be re-composed from retained data — all the day's fetches
    (_fetch_world_today / _fetch_health_chunks / _fetch_today_located_events /
    _fetch_daily_actions) are date-anchored, so back-filling reconstructs that
    day rather than today's snapshot.
    """
    date_override = os.getenv("ESTORMI_BRIEFING_DATE", "").strip()
    if date_override:
        today_date = date.fromisoformat(date_override)
        log.info("Briefing date overridden to %s (back-fill)", today_date)
    else:
        today_date = datetime.now(LOCAL_TZ).date()
    return today_date, today_date.isoformat()


async def _summarise_sources(
    sources: list[dict], today_date: date, today: str, provider: str, model: str
) -> dict:
    """Read today's world-corpus chunks and summarise each configured source.

    Reads today's ``world``-corpus chunks (ingested by the ``knowledge`` pipeline
    stage, ingest_world.py) and reassembles them per source — the briefing no
    longer fetches transcripts/articles itself, it composes from what the
    Ingestion engine already stored. Sources are independent, so each is
    summarised concurrently under a bounded semaphore and their partials merged;
    a single source failing returns None and is skipped, never aborting the
    others. Returns the merged collection counters plus ``world_chunks`` and
    ``world_items_available`` for the collapse-vs-quiet-day decisions downstream.
    """
    world_chunks = await _fetch_world_today(today_date)
    world_by_key = _group_world_items(world_chunks)
    log.info(
        "world corpus: %d chunk(s) → %d item(s) across %d source(s)",
        len(world_chunks),
        sum(len(v) for v in world_by_key.values()),
        len(world_by_key),
    )
    # Whether any *configured* source had world material to summarise this
    # run. When this is true but the summaries below yield nothing, the LLM
    # collapsed (transient outage) rather than there being a quiet day —
    # the two are handled differently so an outage never ships as "ok".
    world_items_available = any(world_by_key.get(source_key(s)) for s in sources)

    _sem = asyncio.Semaphore(_SOURCE_CONCURRENCY)

    async def _guarded(src: dict) -> dict | None:
        src_items = world_by_key.get(source_key(src), [])
        if not src_items:
            return None
        async with _sem:
            try:
                return await _summarize_world_source(src, src_items, provider, model, today)
            except Exception as exc:
                log.warning("source %s failed: %s", src.get("id"), exc)
                return None

    items: list[dict] = []
    total_videos = 0
    rss_articles_count = 0
    youtube_videos_count = 0
    for partial in await asyncio.gather(*[_guarded(s) for s in sources]):
        if not partial:
            continue
        items.extend(partial["items"])
        total_videos += partial["total"]
        rss_articles_count += partial["rss_articles"]
        youtube_videos_count += partial["youtube_videos"]

    return {
        "items": items,
        "total_videos": total_videos,
        "rss_articles_count": rss_articles_count,
        "youtube_videos_count": youtube_videos_count,
        "world_chunks": world_chunks,
        "world_items_available": world_items_available,
    }


async def _synthesize_sections(
    db: aiosqlite.Connection,
    items: list[dict],
    actions: dict,
    today: str,
    provider: str,
    model: str,
) -> tuple[str, str, str]:
    """Run the cross-source editorial passes (news + themes) and build the
    day-vision news digest.

    Returns ``(news_synthesis, themes_html, news_digest)``. Each synthesis LLM
    failure degrades to an empty section rather than aborting the run — the
    day-vision + actions are worth shipping on their own. Topic snippets are
    persisted unconditionally so the next run always has continuity data, and
    the whole-corpus follow-up search is folded into ``news_digest`` so the
    day-vision can frame a developing story against its history.
    """
    news_synthesis = ""
    themes_html = ""
    if items:
        last_briefing_topics = await _get_setting(db, "knowledge_last_briefing_topics", "")
        personal_context = _personal_context_block(
            actions.get("calendar") or [], last_briefing_topics
        )
        # Split BEFORE consolidation so per-source attribution is preserved for
        # news synthesis. News items go straight to _synthesize_news (which is
        # itself the cross-source editorial pass). Only non-news items need the
        # within-group consolidation step first.
        news_items = [item for item in items if item.get("axis") == "news"]
        other_items = [item for item in items if item.get("axis") != "news"]
        log.info("items split: %d news, %d other", len(news_items), len(other_items))
        if news_items:
            # A synthesis LLM failure must degrade to an empty section, not
            # abort the whole briefing — the day-vision + actions below have
            # already succeeded and are worth shipping on their own.
            try:
                news_synthesis = await _synthesize_news(
                    news_items, today, provider, model, personal_context, last_briefing_topics
                )
            except Exception as exc:
                log.warning("news synthesis failed — section omitted: %s", exc)
                news_synthesis = ""
            log.info("news_synthesis: %d chars", len(news_synthesis or ""))
            # Persist topic snippets unconditionally so the next run always
            # has continuity data — even when the synthesis LLM returned "".
            topics_items = news_items
        else:
            # No news-axis items this run (e.g. LLM parse failures on news
            # sources). Fall back to all items so continuity data is still
            # written and "↩ Follow-up:" can surface tomorrow.
            log.warning("No news-axis items — falling back to all items for topics snapshot")
            topics_items = items

        topics = _extract_topics_from_items(topics_items)
        if topics:
            await _put_setting(
                db,
                "knowledge_last_briefing_topics",
                "; ".join(topics),
            )
            log.info("Persisted %d briefing topics for next-run continuity", len(topics))
        if other_items:
            try:
                other_items = await _consolidate_items(other_items, provider, model)
                themes_html = await _synthesize_themes(other_items, today, provider, model)
            except Exception as exc:
                log.warning("theme synthesis failed — section omitted: %s", exc)
                themes_html = ""

    # Past follow-up: search the *whole* world corpus for earlier
    # coverage of today's topics so the day-vision can frame a developing
    # story against its history (not just today's snapshot). Folded into the
    # day-vision's news digest — best-effort, never blocks the briefing.
    news_digest = news_synthesis
    followup_query = "; ".join(_extract_topics_from_items(items)) if items else ""
    if followup_query:
        past = await _fetch_world_followup(followup_query)
        past_lines = [
            f"- {(c.get('title') or '').strip()}: {(c.get('text') or '').strip()[:200]}"
            for c in past
            if (c.get("text") or "").strip()
        ]
        if past_lines:
            news_digest = (
                f"{news_synthesis}\n\nEARLIER COVERAGE (background, for continuity):\n"
                + "\n".join(past_lines)
            ).strip()
            log.info("world follow-up: %d past item(s) added to day-vision", len(past_lines))

    return news_synthesis, themes_html, news_digest


async def _run_critic_repair(
    db: aiosqlite.Connection,
    today: str,
    actions: dict,
    news_digest: str,
    provider: str,
    model: str,
) -> tuple[str, dict, list[dict]]:
    """Critic→repair loop (best-of-N on the day-vision).

    Generate the vision, critique it; if the critic flags issues and repair
    budget remains, regenerate WITH the critic's feedback and re-critique,
    keeping the candidate with the fewest issues. This selects a better draft
    rather than fusing several — it costs one extra vision+critic pass per
    repair (the user opts into the longer run for the quality gain). Disable
    with briefing_repair_attempts=0; capped at 3. Persists the final critique
    and returns ``(vision_html, vision_rows, severe_issues)`` — the rows carry
    the located events the timeline strip is rendered from, and
    ``severe_issues`` is the subset of the FINAL unresolved issues whose type is
    in :data:`_SEVERE_ISSUE_TYPES` (high-confidence factual contradictions the
    caller's overwrite guard acts on). Empty when the best draft is factually
    clean — style/lint/advisory issues never appear here.
    """
    home_location = await _get_setting(db, "briefing_home_location", "Paris, France")
    extractor_model = await _get_setting(db, "briefing_extractor_model", _HAIKU_MODEL)
    critic_model = await _get_setting(db, "briefing_critic_model", _HAIKU_MODEL)
    partner_name = runtime.partner_name

    try:
        repair_attempts = int(await _get_setting(db, "briefing_repair_attempts", "1"))
    except (TypeError, ValueError):
        repair_attempts = 1
    repair_attempts = max(0, min(repair_attempts, 3))

    # Best-of-N (lede + thread paragraphs) and the wall-clock budget bounding
    # it. The run happily takes ~1h in the background; the budget exists so a
    # slow day degrades to N=1 instead of overshooting it.
    try:
        bestof_n = int(await _get_setting(db, "briefing_bestof", "2"))
    except (TypeError, ValueError):
        bestof_n = 2
    bestof_n = max(1, min(bestof_n, 4))
    try:
        budget_min = float(await _get_setting(db, "briefing_time_budget_min", "50"))
    except (TypeError, ValueError):
        budget_min = 50.0
    budget = TimeBudget(budget_min)

    def _critic_llm(p: str):
        # Local decode options (inert for claude-cli): greedy, schema-grammar
        # JSON so the local critic's verdict always parses. The stage routes
        # the local critic to the OTHER quill in two-quills mode — a judge
        # from the writer's own family over-trusts its own voice.
        return runtime._llm_call(
            p,
            provider,
            critic_model,
            max_tokens=800,
            temperature=0.0,
            json_schema=CRITIC_JSON_SCHEMA,
            stage="critic",
        )

    # Fact-critic: verifies the draft against the (capped) vision rows. On by
    # default — ``briefing_fact_critic=0`` is the kill-switch if the extra
    # ~2 min/run (worst case ~10 with the repair it triggers) ever hurts.
    fact_enabled = (await _get_setting(db, "briefing_fact_critic", "1")).strip() != "0"

    # Composer: the local provider's plan-then-write path (selection under an
    # ID-locked grammar, per-thread writers, code-written attributions).
    # ``briefing_composer=single`` is the kill-switch back to the mega-prompt.
    composer_enabled = (
        await _get_setting(db, "briefing_composer", "plan")
    ).strip().lower() != "single"

    def _fact_llm(p: str):
        # Bigger prefill than the structural critic (the data pack rides
        # along) — 300s would be tight on the local provider.
        return runtime._llm_call(
            p,
            provider,
            critic_model,
            max_tokens=600,
            temperature=0.0,
            json_schema=FACT_CRITIC_JSON_SCHEMA,
            timeout=480.0,
            stage="fact_critic",
        )

    vision_html = ""
    critique: dict = {"issues": [], "approved": True}
    feedback = ""
    best_issue_count: int | None = None
    last_rows: dict = {}
    for attempt in range(repair_attempts + 1):
        candidate, vision_rows = await _generate_day_vision(
            today,
            actions,
            provider,
            model,
            news_digest=news_digest,
            home_location=home_location,
            extractor_model=extractor_model,
            critic_feedback=feedback,
            use_composer=composer_enabled,
            bestof_n=bestof_n,
            budget=budget,
        )
        last_rows = vision_rows or last_rows
        if not candidate:
            break
        cand_critique = await critique_briefing(
            candidate, actions.get("calendar") or [], partner_name, _critic_llm
        )
        # Merge the deterministic checks with the LLM critics' judgment
        # issues — all feed the same repair pass:
        #   · structure lint (labels, bullets, language) — zero LLM cost;
        #   · date lint — a draft date that exists nowhere in the data is a
        #     moved deadline, provable in code;
        #   · fact-critic — inverted relations/statuses and misattributed
        #     facts, verified against the very rows the writer saw.
        lint_issues = lint_vision(candidate, language=runtime.language)
        # Coherence (advisory): flag when the objective is built on a distinctive
        # entity/code the MY DAY body never mentions — a divergence the repair
        # pass can realign. Reuse the note-builder's own objective/body split so
        # the check sees exactly what the renderer will. Never blocks shipping;
        # rides the same best-draft path as every other lint issue.
        _fields = briefing_fields(candidate)
        lint_issues += objective_body_divergence(
            _fields["objective"], _fields["myDay"], language=runtime.language
        )
        date_issues = (
            lint_dates(candidate, allowed_date_set(vision_rows, news_digest, today))
            if vision_rows
            else []
        )
        date_issues += lint_weekdays(candidate, today)
        fact_issues: list[dict] = []
        fact_result: dict = {}
        if fact_enabled and vision_rows:
            fact_result = await fact_critique_briefing(candidate, vision_rows, today, _fact_llm)
            fact_issues = fact_result.get("issues") or []
            if fact_issues:
                log.info("fact critic: %d issue(s)", len(fact_issues))
        merged = [*lint_issues, *date_issues, *fact_issues, *(cand_critique.get("issues") or [])]
        if merged:
            cand_critique = {**cand_critique, "issues": merged, "approved": False}
        # Real-defect count drives the repair loop; a critic outage is advisory
        # only. Count them before appending the advisory so an unreachable
        # critic never forces the loop to burn its full repair budget on a draft
        # that is otherwise clean.
        n_issues = len(cand_critique.get("issues") or [])
        # Observability: when a critic ran but returned nothing usable (outage,
        # truncated JSON), surface a non-blocking `critic_unavailable` issue so
        # the run doesn't report an unchecked briefing as silently approved.
        for _critic, _res in (("structural", cand_critique), ("fact", fact_result)):
            if _res.get("critic_error"):
                log.warning("briefing critic advisory: %s critic unavailable", _critic)
                cand_critique = {
                    **cand_critique,
                    "issues": [
                        *(cand_critique.get("issues") or []),
                        {
                            "type": "critic_unavailable",
                            "excerpt": f"{_critic}: {_res['critic_error']}",
                        },
                    ],
                }
        log.info(
            "day_vision attempt %d/%d: %d critic issue(s)",
            attempt + 1,
            repair_attempts + 1,
            n_issues,
        )
        if best_issue_count is None or n_issues < best_issue_count:
            vision_html, critique, best_issue_count = candidate, cand_critique, n_issues
        if n_issues == 0:
            break  # approved — no repair needed
        if attempt < repair_attempts:
            feedback = format_critic_feedback(cand_critique.get("issues") or [])
            log.info("day_vision: requesting repair pass for %d issue(s)", n_issues)
    log.info("vision_html: %d chars (best draft)", len(vision_html or ""))

    # Surgical READINESS repair (local only): the full repair pass keeps
    # re-dumping WHOOP figures; a focused one-line rewrite fixes it cheaply
    # without regenerating the draft. No-op when the line is already a steer.
    if vision_html and provider == "local":
        vision_html = await _condense_readiness_line(vision_html, provider, model)
    # Enforce tutoiement for EVERY provider: the advisory lint+repair loop ships
    # a best draft that may still vouvoie regardless of who composed it. This
    # focused pass fixes only the address and self-skips when there is nothing to
    # fix (zero prose defects), so it is a no-op on a clean cloud draft too.
    if vision_html:
        vision_html = await _repair_voice(vision_html, provider, model)

    severe_issues: list[dict] = []
    if vision_html:
        final_issues = critique.get("issues") or []
        if final_issues:
            for issue in final_issues:
                log.warning(
                    "briefing critic (unresolved): %s — %r",
                    issue.get("type", "unknown"),
                    issue.get("excerpt", ""),
                )
            # Partition off the high-confidence factual contradictions (C1). The
            # caller degrades softly on these — never on style, lint, or the
            # advisory coherence/critic-unavailable issues, which are quality
            # nudges, not falsehoods.
            severe_issues = [
                issue for issue in final_issues if issue.get("type") in _SEVERE_ISSUE_TYPES
            ]
            if severe_issues:
                log.warning(
                    "briefing critic: %d severe fact issue(s) unresolved after repair",
                    len(severe_issues),
                )
        else:
            log.info("briefing critic: approved (no issues)")

    return vision_html, last_rows, severe_issues


def _render_timeline(vision_rows: dict, lang_code: str) -> str:
    """The code-rendered schedule strip for "My day" — bare times + titles +
    free slots, deterministic. The prose above it only carries insights; this
    strip carries the coverage, so nothing the calendar already knows needs an
    LLM sentence."""
    located = (vision_rows or {}).get("located_events") or []
    events: list[dict] = []
    for e in located:
        try:
            start = datetime.fromisoformat(str(e.get("start") or ""))
            end = datetime.fromisoformat(str(e.get("end") or ""))
        except ValueError:
            continue
        # Carry the all-day flag through: an all-day entry parses to a midnight
        # datetime, so only this flag (not the timestamp) tells the renderer to
        # label it "Toute la journée" and pin it to the top of the strip.
        events.append(
            {
                "title": str(e.get("title") or ""),
                "start": start,
                "end": end,
                "all_day": bool(e.get("all_day")),
            }
        )
    if not events:
        return ""
    day = events[0]["start"].astimezone(LOCAL_TZ).date()
    labels = {"free_slot": _t(lang_code, "free_slot"), "all_day": _t(lang_code, "all_day")}
    return timeline_html(events, free_slots(events, day), labels)


async def _persist_and_deliver(
    db: aiosqlite.Connection,
    today: str,
    sources: list[dict],
    actions: dict,
    collected: dict,
    vision_html: str,
    news_synthesis: str,
    themes_html: str,
    model_label: str,
    lang_code: str,
    provider: str,
    model: str,
    timeline_html: str = "",
) -> str:
    """Compose the note, deliver it, and snapshot run metrics.

    Builds the briefing HTML + payload, narrates it to audio, pushes it to the
    vault, writes the ``ok`` run status, and records the composition counters
    into the metric recorder. Returns the summary string.
    """
    total_videos = collected["total_videos"]
    rss_articles_count = collected["rss_articles_count"]
    youtube_videos_count = collected["youtube_videos_count"]
    items = collected["items"]

    body = build_note(
        today,
        len(sources),
        total_videos,
        actions=actions,
        vision_html=vision_html,
        news_synthesis=news_synthesis,
        themes_html=themes_html,
        rss_articles=rss_articles_count,
        youtube_videos=youtube_videos_count,
        model_label=model_label,
        composed_at=datetime.now(LOCAL_TZ).strftime("%H:%M"),
        lang=lang_code,
        timeline_html=timeline_html,
    )
    # The briefing is NOT re-ingested as `briefing`-source chunks: its
    # raw material (the world corpus + personal chunks) is already in the
    # DB and searchable, so storing the composed digest too would just
    # duplicate that content back into retrieval. The briefing is only
    # delivered to the vault below.

    briefing = {
        "id": f"briefing-{today}",
        "date": today,
        "title": briefing_title(today, lang_code),
        "htmlBody": body,
        "sourceCount": len(sources),
        # videoCount is exactly that — YouTube videos. The combined
        # RSS+YouTube total lives nowhere user-facing but the footer,
        # which already prints the split; articleCount carries the RSS
        # half so the field name never lies again.
        "videoCount": youtube_videos_count,
        "articleCount": rss_articles_count,
        "generatedAt": now_iso_z(),
        # The plain-text source of each user-editable prose section (objective,
        # readiness, my-day), so the SPA can offer a structured field editor
        # instead of raw HTML. The briefing PUT endpoint re-renders an edited
        # field through splice_section and swaps it between the matching zone
        # markers in htmlBody. `lang` rides along so that re-render can localise
        # the readiness card chrome without re-reading settings.
        "lang": lang_code,
        "fields": briefing_fields(vision_html),
    }

    # Narration: synthesize the briefing to speech on the Mac (Voxtral, fully
    # on-device) and drop the .m4a next to the JSON so the iOS companion can
    # play it. Best-effort and gated on a setting + the model being present;
    # any failure leaves the briefing text intact, just without audio. Done
    # before the push so the APNs "ready" alert only fires once the audio the
    # companion will play already exists.
    await _maybe_attach_audio(db, today, body, briefing, provider, model)

    # Write the briefing to the iCloud Drive vault so the iOS companion
    # can read it. Failures here are non-fatal — the pipeline continues
    # regardless.
    _vault_push_briefing(briefing, notify=await _decide_notify(db))

    n_actions = sum(len(v) for v in actions.values())
    summary = f"{len(sources)} sources, {total_videos} new items, {n_actions} actions"
    await _put_setting(db, "knowledge_last_run_status", "ok")
    await _put_setting(db, "knowledge_last_run_summary", summary)
    log.info("Done: %s", summary)

    # Snapshot composition into the briefing_runs recorder. Sections
    # map directly onto the build_note kwargs: each integer is the item
    # count, except `vision` / `news` / `themes` which are 0/1 flags
    # (their content is one synthesized HTML block, not enumerable).
    metrics = runtime._run_metrics
    if metrics is not None:
        metrics.set_section("actions", n_actions)
        # `total_videos` is the combined RSS-article + YouTube-video count,
        # so it is recorded under a neutral world-items label rather than
        # "videos" (it would over-count videos on an RSS-heavy day).
        metrics.set_section("world_items", total_videos)
        metrics.set_section("sources", len(sources))
        metrics.set_section("vision", 1 if vision_html else 0)
        metrics.set_section("themes", 1 if themes_html else 0)
        metrics.set_section("news", 1 if news_synthesis else 0)
        # `items` here is the per-source content the engine collected;
        # `items_included` proxies for what shipped in the briefing
        # (actions + world items are the directly-rendered enumerable items;
        # news/themes/vision are synthesized blocks counted via the
        # section flags above).
        metrics.items_considered = len(items)
        metrics.items_included = n_actions + total_videos
    return summary


# How often the auto-observed profile section is re-proposed. Weekly: durable
# facts don't churn faster, and every refresh costs one LLM pass + a settings
# write the user may want to review in the UI.
_PROFILE_REFRESH_DAYS = 7
_PROFILE_SIGNAL_DAYS = 7
_PROFILE_MAX_SIGNALS = 60


async def _maybe_refresh_profile(
    db: aiosqlite.Connection, day: date, provider: str, model: str
) -> None:
    """Weekly LLM refresh of the auto-observed half of the "About you" profile.

    The user's own prose (above the marker) is never touched — only the
    marked auto section is re-proposed from the trailing week of personal
    chunks, then written back to ``briefing_user_context`` where the UI lets
    the user correct or delete it. Disable with ``briefing_profile_auto=0``.
    """
    if (await _get_setting(db, "briefing_profile_auto", "1")).strip() == "0":
        return
    last = await _get_setting(db, "briefing_profile_refreshed_at", "")
    if last:
        try:
            if (day - date.fromisoformat(last[:10])).days < _PROFILE_REFRESH_DAYS:
                return
        except ValueError:
            pass
    current = await _get_setting(db, "briefing_user_context", "")
    user_part, auto_part = split_profile(current)

    chunks = await _fetch_around_mcp(
        {
            "date": day.isoformat(),
            "window_days": _PROFILE_SIGNAL_DAYS,
            "corpus": "personal",
            "limit": 150,
        },
        timeout=20.0,
    )
    signals: list[str] = []
    seen: set[str] = set()
    for c in chunks:
        line = sanitize_chunk(
            " ".join(f"{c.get('title') or ''} {(c.get('text') or '')[:140]}".split())
        )
        key = line.lower()[:60]
        if not line or key in seen:
            continue
        seen.add(key)
        signals.append(f"[{c.get('source') or '?'}] {line[:180]}")
        if len(signals) >= _PROFILE_MAX_SIGNALS:
            break
    if not signals:
        return

    observations = await propose_observations(
        lambda p, **kw: runtime._llm_call(p, provider, model, **kw),
        user_part,
        auto_part,
        signals,
        language=runtime.language,
    )
    await _put_setting(db, "briefing_profile_refreshed_at", day.isoformat())
    if not observations:
        log.info("profile: no grounded observations this week — auto section unchanged")
        return
    lang_code = "fr" if runtime.language.lower().startswith("fr") else "en"
    merged = merge_profile(user_part, observations, lang_code)
    if merged != current:
        await _put_setting(db, "briefing_user_context", merged)
        runtime.user_context = merged
    log.info("profile: auto-observed section refreshed (%d observation(s))", len(observations))


async def run(config_path: Path | None = None) -> str:
    """Run the full briefing pipeline. Returns a summary string.

    Thin orchestrator: it owns the global try/except/finally and run-status
    bookkeeping, and walks the pipeline phases — resolve config, summarise
    sources, synthesize the cross-source sections, run the day-vision
    critic→repair loop, then compose + deliver — each extracted into a named
    helper above. Two collapse guards short-circuit when world material existed
    but every LLM pass failed, so a transient outage never overwrites a good
    vault briefing with a near-empty one.
    """
    log.info("Starting briefing pipeline (DB: %s)", DB_PATH)
    pipeline_start = time.monotonic()
    pipeline_budget = PIPELINE_BUDGET_S

    def _budget_exceeded() -> bool:
        if pipeline_budget <= 0:
            return False
        return (time.monotonic() - pipeline_start) > pipeline_budget

    explicit_config = config_path is not None

    # timeout=30: the server's write bursts can hold the file lock for a few
    # seconds — the 5s sqlite3 default killed a whole run on first contention.
    db = await aiosqlite.connect(DB_PATH, timeout=30.0)
    db.row_factory = aiosqlite.Row

    runtime._run_metrics = None
    persisted_status: str = "error"
    persisted_summary: str = ""

    try:
        await _put_setting(db, "knowledge_last_run_at", now_iso_z())
        await _put_setting(db, "knowledge_last_run_status", "running")
        await _put_setting(db, "knowledge_last_run_summary", "")

        provider, model, model_label, lang_code, config_path = await _resolve_run_config(
            db, config_path, explicit_config
        )

        if not config_path.exists():
            summary = (
                f"No knowledge sources configured (missing {config_path}). "
                "Add sources in the Briefing page or place a knowledge_sources.yaml "
                "in the data directory."
            )
            log.warning(summary)
            await _put_setting(db, "knowledge_last_run_status", "skipped")
            await _put_setting(db, "knowledge_last_run_summary", summary)
            persisted_status, persisted_summary = "skipped", summary
            return summary
        sources = load_sources(config_path)
        today_date, today = _resolve_today()

        collected = await _summarise_sources(sources, today_date, today, provider, model)
        items = collected["items"]
        world_chunks = collected["world_chunks"]
        world_items_available = collected["world_items_available"]

        actions = await _fetch_daily_actions(db, today_date)

        if not items and not any(actions.values()):
            if world_items_available:
                # Material existed but every summary failed and there are no
                # actions to fall back on — a collapse, not a quiet day. Mark
                # it an error so the failure is visible and a retry is invited,
                # and leave any previous vault briefing untouched.
                summary = _collapse_summary(len(world_chunks))
                log.error(summary)
                await _put_setting(db, "knowledge_last_run_status", "error")
                await _put_setting(db, "knowledge_last_run_summary", summary[:200])
                persisted_status, persisted_summary = "error", summary[:200]
                return summary
            summary = f"{len(sources)} sources, 0 new items (last 24h)"
            await _put_setting(db, "knowledge_last_run_status", "ok")
            await _put_setting(db, "knowledge_last_run_summary", summary)
            persisted_status, persisted_summary = "ok", summary
            log.info("No new videos — note skipped")
            return summary

        news_synthesis, themes_html, news_digest = await _synthesize_sections(
            db, items, actions, today, provider, model
        )

        if _budget_exceeded():
            log.warning("pipeline wall-clock budget exceeded after synthesis — shipping partial")
            summary = await _persist_and_deliver(
                db,
                today,
                sources,
                actions,
                collected,
                "",
                news_synthesis,
                themes_html,
                model_label,
                lang_code,
                provider,
                model,
            )
            persisted_status, persisted_summary = "ok", summary
            return summary

        vision_html, vision_rows, severe_issues = await _run_critic_repair(
            db, today, actions, news_digest, provider, model
        )
        tl_html = _render_timeline(vision_rows, lang_code)

        # Collapse guard: world material was available but every summary failed
        # AND the day-vision failed too, so the only thing left to render would
        # be the raw action lists — the near-empty briefing a transient LLM
        # outage produces. Don't overwrite a previously-good vault briefing with
        # that; mark the run an error so it's visible and the next run retries.
        if world_items_available and not items and not vision_html:
            summary = _collapse_summary(len(world_chunks))
            log.error(summary)
            await _put_setting(db, "knowledge_last_run_status", "error")
            await _put_setting(db, "knowledge_last_run_summary", summary[:200])
            if runtime._run_metrics is not None:
                runtime._run_metrics.items_considered = len(items)
                runtime._run_metrics.items_included = 0
            persisted_status, persisted_summary = "error", summary[:200]
            return summary

        # Severe-issue soft-gate (C1): the best draft still carries a
        # high-confidence factual contradiction (inverted relation/status, or a
        # misattributed fact) the repair loop could not resolve. Degrade softly,
        # mirroring the collapse guard: if a previous vault briefing already
        # exists, keep it rather than overwrite it with a draft we know states
        # something false — flag the run an error naming the issue. If NO
        # previous briefing exists, SHIP the flawed draft anyway: a briefing
        # that is wrong on one fact beats leaving the user with nothing. Gated
        # ONLY on the three severe fact types; style/lint/advisory never reach
        # here.
        if severe_issues and _previous_briefing_exists():
            kinds = ", ".join(sorted({str(i.get("type") or "?") for i in severe_issues}))
            summary = (
                f"Briefing suppressed: {len(severe_issues)} unresolved factual "
                f"issue(s) ({kinds}) — previous briefing preserved"
            )
            log.error(summary)
            await _put_setting(db, "knowledge_last_run_status", "error")
            await _put_setting(db, "knowledge_last_run_summary", summary[:200])
            if runtime._run_metrics is not None:
                runtime._run_metrics.items_considered = len(items)
                runtime._run_metrics.items_included = 0
            persisted_status, persisted_summary = "error", summary[:200]
            return summary
        if severe_issues:
            # No previous briefing to fall back on — ship the flawed draft (never
            # leave the user with nothing) but log the unresolved contradictions.
            kinds = ", ".join(sorted({str(i.get("type") or "?") for i in severe_issues}))
            log.warning(
                "briefing ships with %d unresolved severe issue(s) (%s) — no previous "
                "briefing to fall back on",
                len(severe_issues),
                kinds,
            )

        summary = await _persist_and_deliver(
            db,
            today,
            sources,
            actions,
            collected,
            vision_html,
            news_synthesis,
            themes_html,
            model_label,
            lang_code,
            provider,
            model,
            timeline_html=tl_html,
        )
        persisted_status = "ok"
        persisted_summary = summary

        # The profile lives: a weekly LLM pass appends auto-observed facts to
        # the "About you" text (user-editable in the UI). Best-effort — the
        # briefing already shipped.
        try:
            await _maybe_refresh_profile(db, today_date, provider, model)
        except Exception:  # noqa: BLE001
            log.warning("profile auto-refresh failed")
        return summary

    except Exception as exc:
        log.exception("Knowledge pipeline failed")
        try:
            await _put_setting(db, "knowledge_last_run_status", "error")
            await _put_setting(db, "knowledge_last_run_summary", str(exc)[:200])
        except Exception as _exc:
            log.warning("Failed to record run status: %s", _exc)
        persisted_status = "error"
        persisted_summary = str(exc)[:200]
        raise
    finally:
        # Always persist the recorder — including failed runs, where the
        # partial timing + token data is still useful for diagnosing where
        # the pipeline broke.
        if runtime._run_metrics is not None:
            try:
                await runtime._run_metrics.persist(
                    db, status=persisted_status, summary=persisted_summary
                )
            except Exception as _exc:  # noqa: BLE001
                log.warning("briefing_runs.persist failed: %s", _exc)
        runtime._run_metrics = None
        await db.close()


def _refresh_mode() -> str:
    """The launcher's refresh knob — ``"health"`` runs the wake-time readiness
    refresh (``refresh_health``) instead of the full pipeline."""
    return os.getenv("ESTORMI_BRIEFING_REFRESH", "").strip().lower()


def _previous_briefing_exists() -> bool:
    """Whether the vault already holds a briefing to fall back on (C1).

    Reuses the vault's own listing so no path logic is duplicated here. On any
    failure — an unconfigured or unreadable vault — returns ``False``, the SAFE
    default: an inability to confirm a fallback must never let the severe-issue
    guard withhold a briefing and leave the user with nothing.
    """
    try:
        return bool(_vault_list_briefings())
    except Exception as exc:  # noqa: BLE001 — degrade-soft: unknown → ship
        log.warning("previous-briefing check failed (%s) — treating as none", exc)
        return False


async def _decide_notify(db) -> bool:
    """Whether a freshly-composed briefing should ring the iOS companion.

    The morning cron composes the briefing *before* the user wakes, so notifying
    on that write would ping ahead of real wake. When the WHOOP wake-trigger is
    enabled it owns morning delivery: the scheduled cron pre-computes silently
    and the poller announces at the detected wake (or, failing that, at its
    window's close — see ``server.schedulers._schedule_whoop_poll``). The
    launcher's ``ESTORMI_BRIEFING_NOTIFY`` overrides this: ``"force"`` (a manual
    run, or a poller-triggered wake run) always announces, ``"silent"`` never.
    """
    mode = os.getenv("ESTORMI_BRIEFING_NOTIFY", "").strip().lower()
    if mode == "force":
        return True
    if mode == "silent":
        return False
    # Default (the daily scheduled cron): stay silent only while the WHOOP
    # wake-trigger is the active deliverer; otherwise announce as before.
    return (await _get_setting(db, "whoop_polling_enabled", "false")) != "true"


async def _run_and_report() -> int:
    """Run the pipeline (or the health refresh) and translate its outcome
    into a process exit code.

    A collapsed run (every LLM pass failed; the only renderable content would
    be raw action lists) returns a summary *normally* but persists
    ``knowledge_last_run_status = "error"``. Without inspecting that, the
    process exits 0, and the briefing launcher — which derives the vault
    engine-history status purely from the child's returncode
    (``estormi_server/server/launchers/briefing.py``) — records a FAILED run as
    ok, so the iOS companion shows a green run that produced nothing. Map a
    persisted ``error`` status to a non-zero exit so the engine history and the
    process exit code agree.
    """
    try:
        if _refresh_mode() == "health":
            from estormi_briefing.refresh_health import run_refresh  # noqa: PLC0415

            await run_refresh()
        else:
            await run()
    except Exception:
        # run() already logged + persisted "error"; the traceback is noise here.
        log.exception("briefing entrypoint failed")
        return 1
    db = await aiosqlite.connect(DB_PATH, timeout=30.0)
    try:
        cur = await db.execute("SELECT value FROM settings WHERE key = 'knowledge_last_run_status'")
        row = await cur.fetchone()
    finally:
        await db.close()
    return 1 if row and row[0] == "error" else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(_run_and_report()))
