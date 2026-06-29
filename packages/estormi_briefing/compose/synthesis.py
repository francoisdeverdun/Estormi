"""LLM editorial passes — per-source summarisation, consolidation, synthesis.

The briefing-swarm's content passes: summarise each ``world`` source's items
into bullets (:func:`_summarize_world_source`), a within-section consolidation
pass (:func:`_consolidate_items`), and the two cross-source synthesis passes
that fold the bullets into a unified news digest (:func:`_synthesize_news`) and
themed non-news blocks (:func:`_synthesize_themes`). Each falls back to the
deterministic, already-sourced builders in ``prompts`` when a weak
model ignores the citation/structure format.

The metric-aware LLM call is reached through the ``runtime`` module at call
time so the orchestrator's per-run recorder and the test suite's single
``runtime._llm_call`` patch target both apply.
"""

from __future__ import annotations

import json
import os
import re

import structlog

from estormi_briefing.compose.exemplars import exemplar_block
from estormi_briefing.compose.prompts import (
    BULLETS_JSON_SCHEMA,
    _consolidation_prompt,
    _format_rss_articles,
    _make_prompt,
    _make_rss_prompt,
    _news_synthesis_prompt,
    _numbered_news,
    _parse_bullets,
    _themes_prompt,
    fallback_news_from_items,
    fallback_themes_from_items,
    resolve_news_citations,
)
from estormi_briefing.compose.user_profile import distinctive_tokens, impact_grounded
from estormi_briefing.lint.fact_lint import normalised_key, numbers_not_in_source
from estormi_briefing.llm import runtime

log = structlog.get_logger()

# Local GGUFs have a small context window; the CLI providers are generous but
# still get a hard cap so a pathological multi-hour transcript can't blow up
# latency/cost on a single call.
_LOCAL_MAX_CHARS = 18_000
_CLI_MAX_CHARS = 200_000

# Max sources summarised concurrently. Each source may fire several LLM calls
# (one per video), so the bound keeps a large feed list from spawning dozens of
# parallel CLI subprocesses at once. Tunable via env.
_SOURCE_CONCURRENCY = int(os.getenv("BRIEFING_SOURCE_CONCURRENCY", "4"))

# Local GGUFs choke on a large single-shot RSS batch (e.g. a large RSS feed of ~60
# articles → 0 parseable bullets): the prompt gets truncated and the model
# can't emit a long valid JSON array. Summarise the batch in small sub-batches
# for the local provider so each prompt stays within reach; cloud models keep
# the single call (they handle the full batch and cross-reference better in one
# pass). Articles per local sub-batch:
_RSS_LOCAL_BATCH = int(os.getenv("BRIEFING_RSS_LOCAL_BATCH", "12"))

# Per-task decode options for the LOCAL provider (claude-cli ignores them).
# Reply budgets sized to each pass's real output — the old flat 1024-token
# default truncated the long passes mid-sentence. Sampling: extraction-style
# passes stay greedy (faithfulness), the cross-source editorial passes get a
# little temperature so the prose doesn't collapse into flat repetition.
_NEWS_SYNTH_OPTS = {"max_tokens": 1200, "temperature": 0.1, "stage": "news_synthesis"}
# 1400: the themes reply (THEME:/SOURCE: blocks, 2-4 sentences each) hit a
# 1000-token cap on a 4-source day and lost its tail mid-block.
_THEMES_OPTS = {"max_tokens": 1400, "temperature": 0.1, "stage": "themes"}
# The JSON-emitting passes additionally decode under a schema grammar locally,
# so their replies always parse (a 14B drifts out of bare-instruction JSON).
# Budgets leave headroom over the schema's worst case (6 items × a few
# sentences) — a grammar reply that hits max_tokens loses its closing
# brackets, and even the truncation-repair parse then drops the last item.
_CONSOLIDATION_OPTS = {
    "max_tokens": 1100,
    "temperature": 0.0,
    "json_schema": BULLETS_JSON_SCHEMA,
    "stage": "consolidation",
}
_SUMMARY_OPTS = {
    "max_tokens": 1100,
    "temperature": 0.0,
    "json_schema": BULLETS_JSON_SCHEMA,
    "stage": "summary",
}


def _maybe_truncate(text: str, provider: str) -> str:
    cap = _LOCAL_MAX_CHARS if provider == "local" else _CLI_MAX_CHARS
    if len(text) <= cap:
        return text
    return text[:cap] + "\n[transcript truncated]"


# An inline "→ Impact: …" clause inside a news bullet — everything up to the
# code-attached [SOURCE: …] marker (or end of line).
_IMPACT_CLAUSE_RE = re.compile(r"\s*→\s*Impact\s*:?[^\[\n]*")


def _cap_impact_lines(text: str, cap: int) -> str:
    """Enforce the prompt's 'AT MOST N items carry an impact line' in code.

    The synthesis prompt orders bullets by relevance and asks for at most 3
    impact lines; a weak model decorates every item with a thematic-echo
    "impact" anyway. Keep the clause on the first ``cap`` impact-bearing
    bullets (the most relevant ones) and strip it from the rest — a bare item
    beats a forced connection."""
    out: list[str] = []
    kept = 0
    for line in text.splitlines():
        if _IMPACT_CLAUSE_RE.search(line):
            if kept < cap:
                kept += 1
            else:
                line = re.sub(r"\s{2,}", " ", _IMPACT_CLAUSE_RE.sub(" ", line)).rstrip()
        out.append(line)
    return "\n".join(out)


def _strip_ungrounded_impacts(text: str) -> str:
    """Drop ``→ Impact`` clauses that share no distinctive vocabulary with the
    user's profile (the "About you" text).

    The impact line is the world→personal bridge — and the channel a weak
    model forces thematic echoes through ("SpaceX → tes projets tech"). An
    impact that can't trace a single content word back to the profile is an
    invented hook; with no profile at all, every impact is ungroundable and
    they all go (a bare fact beats a fake connection — the briefing's honesty
    rule)."""
    tokens = distinctive_tokens(runtime.user_context)
    out: list[str] = []
    for line in text.splitlines():
        m = _IMPACT_CLAUSE_RE.search(line)
        if m and not (tokens and impact_grounded(m.group(0), tokens)):
            line = re.sub(r"\s{2,}", " ", _IMPACT_CLAUSE_RE.sub(" ", line)).rstrip()
        out.append(line)
    return "\n".join(out)


# ── correlation floor + deterministic continuity ──────────────────────────────
# The world section's correlation layer is its impact lines and follow-up
# markers — and exactly the channel a weak model fails to emit (the 2026-06-12
# bench: Gemma shipped zero of either, Ministral two impacts). Per skeleton-v2
# doctrine the COVERAGE belongs to code: follow-ups are marked
# deterministically from the persisted topic snapshot, and a section under the
# impact floor earns ONE targeted repair call. The honesty rule still wins —
# an added impact must pass the same profile grounding, so a fake hook never
# ships just to meet the quota.
_NEWS_MIN_IMPACTS = int(os.getenv("BRIEFING_NEWS_MIN_IMPACTS", "2"))
_IMPACT_REPAIR_SCHEMA = {
    "type": "object",
    "properties": {
        "impacts": {
            "type": "array",
            "maxItems": 3,
            "items": {
                "type": "object",
                "properties": {
                    "index": {"type": "integer"},
                    "impact": {"type": "string"},
                },
                "required": ["index", "impact"],
            },
        }
    },
    "required": ["impacts"],
}
_IMPACT_REPAIR_OPTS = {
    "max_tokens": 500,
    "temperature": 0.0,
    "json_schema": _IMPACT_REPAIR_SCHEMA,
    "stage": "impact_repair",
}
# Minimum distinctive tokens (prefix-6) a bullet must share with one persisted
# topic before it is marked as its follow-up — a single shared token
# ("France") is coincidence, not continuity.
_FOLLOWUP_MIN_SHARED = 2


def _count_impacts(text: str) -> int:
    return sum(1 for line in (text or "").splitlines() if _IMPACT_CLAUSE_RE.search(line))


def mark_followups(text: str, last_topics: str) -> str:
    """Deterministically prefix bullets continuing yesterday's topics.

    The synthesis prompt asks the model to mark them; cloud models comply,
    local ones often don't. Continuity is provable from the persisted topic
    snapshot, so code now guarantees it: a bullet sharing
    ``_FOLLOWUP_MIN_SHARED`` distinctive tokens with one of yesterday's
    topics gets the canonical "↩ Follow-up: " prefix (already-marked and
    📅-flagged bullets pass through untouched).
    """
    topic_tokens = [
        {tok[:6] for tok in distinctive_tokens(t.strip())}
        for t in (last_topics or "").split(";")
        if t.strip()
    ]
    topic_tokens = [t for t in topic_tokens if t]
    if not topic_tokens:
        return text
    out: list[str] = []
    marked = 0
    for line in (text or "").splitlines():
        stripped = line.lstrip()
        if (
            stripped.startswith(("- ", "• "))
            and "↩" not in stripped
            and not stripped[2:].lstrip().startswith("📅")
        ):
            body = _SOURCE_MARKER_STRIP_RE.sub("", stripped[2:])
            prefixes = {tok[:6] for tok in distinctive_tokens(body)}
            if any(len(prefixes & t) >= _FOLLOWUP_MIN_SHARED for t in topic_tokens):
                indent = line[: len(line) - len(stripped)]
                line = f"{indent}{stripped[:2]}↩ Follow-up: {stripped[2:]}"
                marked += 1
        out.append(line)
    if marked:
        log.info("followups: %d bullet(s) marked from the topic snapshot", marked)
    return "\n".join(out)


async def _ensure_impact_floor(resolved: str, provider: str, model: str, cap: int) -> str:
    """One targeted repair call when the section carries too few impact lines.

    Runs AFTER the grounding strip, so it only ever tops up what honestly
    survived. The repair's own clauses go through the same
    :func:`impact_grounded` gate — with no profile, or no genuinely-linked
    item, the section ships bare (a missing impact beats a forced one).
    """
    tokens = distinctive_tokens(runtime.user_context)
    if not tokens:
        return resolved
    floor = min(_NEWS_MIN_IMPACTS, cap)
    have = _count_impacts(resolved)
    if have >= floor:
        return resolved
    lines = resolved.splitlines()
    bullet_lines = [i for i, ln in enumerate(lines) if ln.lstrip().startswith(("- ", "• "))]
    candidates = [
        (n, _SOURCE_MARKER_STRIP_RE.sub("", lines[i].lstrip()[2:]).strip())
        for n, i in enumerate(bullet_lines)
        if not _IMPACT_CLAUSE_RE.search(lines[i])
    ]
    if not candidates:
        return resolved
    need = floor - have
    numbered = "\n".join(f"[{n}] {t}" for n, t in candidates)
    prompt = (
        "These are today's world-news items from a personal morning briefing, "
        "and the user's profile.\n\n"
        f"PROFILE:\n{runtime.user_context}\n\n"
        f"ITEMS:\n{numbered}\n\n"
        f"Pick AT MOST {need} item(s) with the strongest DIRECT, concrete "
        "consequence for this user's own life, work or budget, and state that "
        f"consequence in ONE short sentence each, in {runtime.language}, "
        "addressing the user as « tu ». The link must be rooted in the "
        "profile's own vocabulary — when no item truly touches the user, "
        "return an empty list rather than inventing a connection.\n"
        'Reply with JSON only: {"impacts": [{"index": <item number>, '
        '"impact": "<sentence>"}]}'
    )
    try:
        raw = await runtime._llm_call(prompt, provider, model, **_IMPACT_REPAIR_OPTS)
        impacts = json.loads(raw).get("impacts") or []
    except Exception as exc:  # noqa: BLE001 — floor is best-effort
        log.warning("impact floor: repair call failed (%r) — section ships as is", exc)
        return resolved
    added = 0
    for item in impacts:
        idx, clause = item.get("index"), " ".join(str(item.get("impact") or "").split())
        clause = clause.rstrip(".")
        if not isinstance(idx, int) or not (0 <= idx < len(bullet_lines)) or not clause:
            continue
        line = lines[bullet_lines[idx]]
        if _IMPACT_CLAUSE_RE.search(line):
            continue  # the model picked a bullet that already carries one
        if not impact_grounded(clause, tokens):
            log.info("impact floor: ungrounded repair clause dropped (%.60s)", clause)
            continue
        if have + added >= cap:
            break
        m = re.search(r"\s*\[SOURCE:", line)
        insertion = f" → Impact: {clause}."
        lines[bullet_lines[idx]] = (
            line[: m.start()] + insertion + line[m.start() :] if m else line + insertion
        )
        added += 1
    if added:
        log.info("impact floor: %d impact line(s) added by targeted repair", added)
    return "\n".join(lines)


# Coverage floor/ceiling for the world section. Citation-resolve correctly
# drops ungrounded bullets — but shipping 4 world items on a 37-article day is
# a coverage collapse, not editing. The floor backfills from the REAL input
# bullets (already sourced, hallucination-free); the ceiling trims a rambling
# day back to a finishable list.
_NEWS_MIN_BULLETS = int(os.getenv("BRIEFING_NEWS_MIN_BULLETS", "6"))
_NEWS_MAX_BULLETS = int(os.getenv("BRIEFING_NEWS_MAX_BULLETS", "10"))
_SOURCE_MARKER_STRIP_RE = re.compile(r"\s*\[SOURCE:[^\]]*\]", re.IGNORECASE)


def _bullet_key(line: str) -> str:
    return normalised_key(_SOURCE_MARKER_STRIP_RE.sub("", line))


def _enforce_news_bounds(resolved: str, news_items: list[dict], date_str: str) -> str:
    """Backfill a thin world section from the input bullets; trim a bloated one.

    Order is preserved (the model leads with the most relevant); backfill
    appends, so synthesized cross-referenced bullets always outrank the
    deterministic extras."""
    lines = resolved.splitlines()
    bullet_count = sum(1 for ln in lines if ln.lstrip().startswith(("- ", "• ")))
    if bullet_count > _NEWS_MAX_BULLETS:
        out: list[str] = []
        kept = 0
        for ln in lines:
            if ln.lstrip().startswith(("- ", "• ")):
                kept += 1
                if kept > _NEWS_MAX_BULLETS:
                    continue
            out.append(ln)
        log.info("news bounds: trimmed %d → %d bullet(s)", bullet_count, _NEWS_MAX_BULLETS)
        return "\n".join(out)
    if bullet_count >= _NEWS_MIN_BULLETS:
        return resolved
    seen = {_bullet_key(ln) for ln in lines if ln.lstrip().startswith(("- ", "• "))}
    extras: list[str] = []
    for ln in fallback_news_from_items(news_items, date_str, limit=24).splitlines():
        if bullet_count + len(extras) >= _NEWS_MIN_BULLETS:
            break
        key = _bullet_key(ln)
        if key and key not in seen:
            seen.add(key)
            extras.append(ln)
    if extras:
        log.info(
            "news bounds: backfilled %d bullet(s) (synthesis kept only %d)",
            len(extras),
            bullet_count,
        )
        return (resolved.rstrip() + "\n" + "\n".join(extras)).strip()
    return resolved


async def _bullets_with_real_figures(
    bullets: list[str],
    source_text: str,
    prompt: str,
    provider: str,
    model: str,
    label: str,
) -> list[str]:
    """Drop summary bullets whose figures don't exist in the source.

    The worst world-section errors are numbers the summariser invents or
    distorts ("sous 60 000 $" for a 75 000 $ price). Every unit-bearing
    figure in a bullet must have its digits somewhere in the source text —
    a provable check. One corrective re-ask first; bullets that still carry
    phantom figures are dropped (a missing item beats a false one).
    """
    flagged = {b: v for b in bullets if (v := numbers_not_in_source(b, source_text))}
    if not flagged:
        return bullets
    bad_figures = sorted({f for v in flagged.values() for f in v})
    log.info(
        "%s: %d bullet(s) cite figures absent from source %s — retrying",
        label,
        len(flagged),
        bad_figures,
    )
    retry_prompt = (
        f"{prompt}\n\nREMINDER — a previous attempt was rejected because it cited "
        f"figures absent from the source: {', '.join(bad_figures)}. Every figure in "
        "your reply must exist VERBATIM in the provided content; never round, "
        "convert or invent a number."
    )
    try:
        output = await runtime._llm_call(retry_prompt, provider, model, **_SUMMARY_OPTS)
        retried = _parse_bullets(output)
    except Exception as exc:  # noqa: BLE001 — best-effort; fall through to dropping
        log.warning("%s: figure-retry failed: %s", label, exc)
        retried = []
    # The unflagged originals were never wrong — they always survive. The
    # retry only gets to REPLACE the flagged ones: its clean bullets join,
    # minus near-duplicates of what is already kept.
    kept = [b for b in bullets if b not in flagged]
    kept_keys = {normalised_key(b) for b in kept}
    replacements = [
        b
        for b in retried
        if not numbers_not_in_source(b, source_text) and normalised_key(b) not in kept_keys
    ]
    if replacements:
        log.info("%s: %d flagged bullet(s) replaced by clean retries", label, len(replacements))
    else:
        log.info("%s: dropped %d bullet(s) with phantom figures", label, len(flagged))
    return kept + replacements


async def _synthesize_news(
    news_items: list[dict],
    date_str: str,
    provider: str,
    model: str,
    personal_context: str = "",
    last_topics: str = "",
) -> str:
    """Cross-reference news-axis items and produce a unified bullet list.

    Returns the raw LLM text (dash lines). Raises on LLM failure.
    """
    has_bullets = any(str(b).strip() for item in news_items for b in item.get("bullets", []))
    if not has_bullets:
        return ""
    prompt = _news_synthesis_prompt(news_items, date_str, personal_context)
    # Cloud-quality impact-line anchors (data-dir bank, possibly absent) —
    # the grounding strip downstream still kills any fact leakage.
    examples = exemplar_block("impact", runtime.language)
    if examples:
        prompt = f"{prompt}\n\n{examples}"
    _, sources_index = _numbered_news(news_items, date_str)
    try:
        output = await runtime._llm_call(prompt, provider, model, **_NEWS_SYNTH_OPTS)
    except Exception as exc:
        log.error("News synthesis LLM call failed: %s", exc)
        raise
    # Code attaches the real source/date from the [n] citations the model emits,
    # and drops any uncited (ungrounded) bullet — attribution & anti-hallucination
    # no longer depend on the model formatting [SOURCE:] correctly.
    resolved = resolve_news_citations(output, sources_index)
    kept = sum(1 for ln in resolved.splitlines() if ln.lstrip().startswith(("- ", "• ")))
    if kept == 0:
        # The model ignored the citation format (weak local models do). The
        # deterministic fallback loses the cross-referencing — the briefing's
        # headline value — so give the model ONE more chance with the failure
        # spelled out before settling for it.
        log.info("news synthesis: citation-resolve empty → one retry with format reminder")
        retry_prompt = (
            f"{prompt}\n\n"
            "REMINDER — a previous attempt was rejected because its bullets did "
            "not end with the [n] source citations. Every bullet MUST end with "
            "the number(s) of the source item(s) it draws from, in square "
            "brackets: [3], or [1,4] when merging several. Numbers only, taken "
            "from the <sources> list."
        )
        try:
            output = await runtime._llm_call(retry_prompt, provider, model, **_NEWS_SYNTH_OPTS)
            resolved = resolve_news_citations(output, sources_index)
            kept = sum(1 for ln in resolved.splitlines() if ln.lstrip().startswith(("- ", "• ")))
        except Exception as exc:  # noqa: BLE001 — retry is best-effort
            log.warning("news synthesis retry failed: %s", exc)
            kept = 0
    if kept == 0:
        # Still unusable. Fall back to the real, already-sourced input
        # bullets — no cross-referencing, but a non-empty, hallucination-free
        # section instead of an empty one.
        resolved = fallback_news_from_items(news_items, date_str)
        log.info("news synthesis: citation-resolve empty → deterministic fallback")
    else:
        log.info("news synthesis: %d bullet(s) kept after citation resolve", kept)
    # Coverage floor/ceiling first (real input bullets only), then the
    # correlation layer: follow-ups marked deterministically from the topic
    # snapshot, then the impact leash — a local 14B ignores the prompt's
    # impact budget and force-links every item to the profile — cap the
    # clauses, drop any whose words can't be traced back to the user's own
    # profile, and finally top back up to the impact floor with one targeted
    # (still grounding-gated) repair call when the model under-delivered.
    impact_cap = 2 if provider == "local" else 3
    resolved = _enforce_news_bounds(resolved, news_items, date_str)
    resolved = mark_followups(resolved, last_topics)
    resolved = _cap_impact_lines(resolved, cap=impact_cap)
    resolved = _strip_ungrounded_impacts(resolved)
    return await _ensure_impact_floor(resolved, provider, model, cap=impact_cap)


async def _synthesize_themes(
    other_items: list[dict],
    date_str: str,
    provider: str,
    model: str,
) -> str:
    """Cluster non-news items by theme via a single LLM call.

    Returns sanitizable HTML (only <p>/<b> tags). Raises on LLM failure.
    """
    has_bullets = any(str(b).strip() for item in other_items for b in item.get("bullets", []))
    if not has_bullets:
        return ""
    prompt = _themes_prompt(other_items, date_str)
    try:
        output = await runtime._llm_call(prompt, provider, model, **_THEMES_OPTS)
    except Exception as exc:
        log.error("Theme synthesis LLM call failed: %s", exc)
        raise
    # Trust the model only if it followed the THEME:/SOURCE: structure. Weak
    # local models drift to free markdown ("**Title**", "[Src] · …") which the
    # renderer mangles and which leaks scaffolding — fall back to deterministic,
    # already-sourced per-source blocks in the canonical format.
    if re.search(r"(?mi)^\s*TH[EÈ]ME:", output) and re.search(r"(?mi)^\s*SOURCE:", output):
        return output
    log.info("themes: model output unstructured → deterministic fallback")
    return fallback_themes_from_items(other_items, date_str)


async def _consolidate_items(
    items: list[dict],
    provider: str,
    model: str,
) -> list[dict]:
    """Run a second editorial pass within each rendered section.

    Groups by ``(axis, mode, source_label)`` so each source stays its own
    bucket — the previous ``(axis, mode)`` grouping silently merged sources
    that shared a mode, which dropped the per-source ``pre_prompt`` user
    guidance.
    """
    grouped: dict[tuple[str, str, str], dict] = {}
    for item in items:
        key = (item["axis"], item["mode"], item.get("source_label", ""))
        group = grouped.setdefault(
            key,
            {
                "axis": item["axis"],
                "mode": item["mode"],
                "source_label": item.get("source_label", ""),
                "pre_prompt": item.get("pre_prompt", ""),
                "bullets": [],
            },
        )
        group["bullets"].extend(item.get("bullets", []))

    consolidated: list[dict] = []
    for item in grouped.values():
        bullets = [b for b in item.get("bullets", []) if str(b).strip()]
        if len(bullets) <= 1:
            consolidated.append(item)
            continue

        prompt = _consolidation_prompt(
            item["axis"],
            item["mode"],
            bullets,
            pre_prompt=item.get("pre_prompt", ""),
            source_label=item.get("source_label", ""),
        )
        try:
            output = await runtime._llm_call(prompt, provider, model, **_CONSOLIDATION_OPTS)
            next_bullets = _parse_bullets(output)
        except Exception as exc:
            log.warning(
                "Consolidation LLM call failed for %s/%s: %s",
                item["axis"],
                item["mode"],
                exc,
            )
            next_bullets = []

        consolidated.append({**item, "bullets": next_bullets or bullets})
    return consolidated


async def _summarize_world_source(
    source: dict,
    world_items: list[dict],
    provider: str,
    model: str,
    today: str,
) -> dict:
    """Summarise one source's already-ingested ``world`` items into bullets.

    ``world_items`` are this source's chunks read back from the DB (corpus
    ``world``) and reassembled by :func:`_group_world_items` — the briefing no
    longer fetches transcripts/articles itself. Self-contained (no shared
    state) so the orchestrator can run sources concurrently. Returns
    ``{items, total, rss_articles, youtube_videos}``.
    """
    items: list[dict] = []
    if not world_items:
        return {"items": items, "total": 0, "rss_articles": 0, "youtube_videos": 0}

    if source["type"] == "rss":
        # Re-shape DB items into the {title, summary, published} form the RSS
        # formatter expects, then summarise the batch in one call.
        articles = [
            {
                "title": w.get("title", ""),
                "summary": w.get("text", ""),
                "published": w.get("date", ""),
            }
            for w in world_items
        ]
        # One pass for cloud models (better cross-referencing); small sub-batches
        # for local GGUFs, which otherwise return 0 bullets on a big batch.
        if provider == "local" and len(articles) > _RSS_LOCAL_BATCH:
            batches = [
                articles[i : i + _RSS_LOCAL_BATCH]
                for i in range(0, len(articles), _RSS_LOCAL_BATCH)
            ]
        else:
            batches = [articles]
        bullets: list[str] = []
        for batch in batches:
            content = _format_rss_articles(batch)
            prompt = _make_rss_prompt(source, _maybe_truncate(content, provider), today)
            try:
                output = await runtime._llm_call(prompt, provider, model, **_SUMMARY_OPTS)
            except Exception as exc:
                log.warning(
                    "LLM call failed for RSS source %s (batch of %d): %s",
                    source["id"],
                    len(batch),
                    exc,
                )
                continue
            bullets.extend(
                await _bullets_with_real_figures(
                    _parse_bullets(output), content, prompt, provider, model, source["id"]
                )
            )
        log.info("%s: %d bullets parsed (%d batch(es))", source["id"], len(bullets), len(batches))
        if bullets:
            items.append(
                {
                    "axis": source["axis"],
                    "mode": source["mode"],
                    "source_label": source["label"],
                    "pre_prompt": source.get("pre_prompt", ""),
                    "bullets": bullets,
                }
            )
        return {
            "items": items,
            "total": len(articles),
            "rss_articles": len(articles),
            "youtube_videos": 0,
        }

    # YouTube (or any transcript-style source): one LLM pass per item, merged
    # into a single per-source bullet list so attribution stays clean.
    youtube_videos = 0
    bullets: list[str] = []
    for w in world_items:
        transcript = (w.get("text") or "").strip()
        if not transcript:
            continue
        prompt = _make_prompt(
            source["mode"],
            source["label"],
            w.get("date") or today,
            _maybe_truncate(transcript, provider),
            source.get("pre_prompt", ""),
            promotional=bool(source.get("promotional")),
        )
        try:
            output = await runtime._llm_call(prompt, provider, model, **_SUMMARY_OPTS)
        except Exception as exc:
            log.warning("LLM call failed for %s/%s: %s", source["id"], w.get("title", "?"), exc)
            continue
        parsed = await _bullets_with_real_figures(
            _parse_bullets(output), transcript, prompt, provider, model, source["id"]
        )
        log.info("%s/%s: %d bullets parsed", source["id"], w.get("title", "?"), len(parsed))
        bullets.extend(parsed)
        youtube_videos += 1

    if bullets:
        items.append(
            {
                "axis": source["axis"],
                "mode": source["mode"],
                "source_label": source["label"],
                # Per-source editorial guidance from the KnowledgeSourcesPanel
                # modal, carried through consolidation + themes so the LLM
                # keeps the user's framing.
                "pre_prompt": source.get("pre_prompt", ""),
                "bullets": bullets,
            }
        )
    return {
        "items": items,
        "total": youtube_videos,
        "rss_articles": 0,
        "youtube_videos": youtube_videos,
    }
