"""Day-vision: the briefing's unifying cross-source pass.

This is where the engine's headline value — *correlation* — is assembled. For
the briefing day it fetches the user's own context (recent WhatsApp, day chunks,
health, upcoming events), enriches it with weather + back-to-back event chains,
anchors a semantic search of the personal corpus to each near-term
event/reminder so related chatter is linked deliberately, and hands the whole
bundle to the LLM as one assistant-style HTML snippet that relates world news to
the user's day.

The metric-aware LLM call is reached through the ``runtime`` module at call
time so the orchestrator's per-run recorder and the test suite's single
``runtime._llm_call`` patch target both apply.
"""

from __future__ import annotations

import asyncio
import os
import re
import unicodedata
from datetime import date, datetime, timedelta
from pathlib import Path

import structlog

from estormi_briefing.compose.composer import ComposerError, compose_vision
from estormi_briefing.compose.continuity import callbacks as continuity_callbacks
from estormi_briefing.compose.continuity import load_state as load_continuity_state
from estormi_briefing.compose.prompts import (
    EXTRACTOR_JSON_SCHEMA,
    VISION_GBNF,
    _assemble_vision_rows,
    _build_vision_prompt,
    extract_day_facts,
)
from estormi_briefing.compose.timeline import free_slots
from estormi_briefing.day.day import (
    LOCAL_TZ,
    _day_anchor,
    _is_all_day_raw,
    _local_when_label,
    _parse_iso_datetime,
)
from estormi_briefing.day.day_context import (
    _CALENDAR_SOURCES,
    _CORR_MAX_EVENTS,
    _fetch_day_context_chunks,
    _fetch_health_chunks,
    _fetch_recent_whatsapp,
    _fetch_upcoming_events,
)
from estormi_briefing.day.day_load import choose_advice, day_features, parse_whoop
from estormi_briefing.io import enrichments
from estormi_briefing.io.mcp_io import _fetch_around_mcp, _search_mcp_memory
from estormi_briefing.lint.fact_lint import extract_deadline_lines, nearest_future_date
from estormi_briefing.lint.vision_lint import (
    lint_vision,
    readiness_has_figure_dump,
    readiness_line_span,
)
from estormi_briefing.llm import runtime
from estormi_briefing.llm.bestof import TimeBudget
from estormi_briefing.llm.llm_dispatch import _HAIKU_MODEL
from memory_core.sanitizer import sanitize_chunk
from memory_core.settings import resolve_data_dir

log = structlog.get_logger()

# Event-anchored correlation, per-event side (the anchors themselves come from
# ``day_context._fetch_upcoming_events``). How many related chunks one event may
# contribute, the relatedness floor, and how far back the search may reach. All
# env-tunable.
_CORR_PER_EVENT = int(os.getenv("BRIEFING_CORRELATION_PER_EVENT", "3"))
# Minimum ABSOLUTE dense cosine [0,1] a correlation candidate must clear, passed
# to search_memory as `min_score` (dense-only relatedness retrieval). Calibrated
# against real fastembed output: a genuine match scores ~0.85/0.63 while
# unrelated same-language text clusters at ~0.43–0.59, so ~0.6 separates them.
# The hybrid `relevance`/`fusion_score` fields can't gate this — they are
# rank-based, so the top hit is always 1.0 even for an unrelated pool.
_CORR_MIN_SIMILARITY = float(os.getenv("BRIEFING_CORRELATION_MIN_SIMILARITY", "0.6"))
# How far back the correlation search may reach. Generous so a chat/mail from a
# few weeks ago about an upcoming event still links — the cosine floor + the
# per-event cap keep it precise, the bound only stops truly ancient matches
# resurfacing as "today". Enforced server-side on date_ts.
_CORR_LOOKBACK_DAYS = int(os.getenv("BRIEFING_CORRELATION_LOOKBACK_DAYS", "90"))
# Events correlated concurrently. Each event now fires TWO searches (semantic +
# lexical arm), and every /search_memory embeds the query — 12 unbounded events
# meant ~24 simultaneous embedding requests, which timed out the local server
# and silently degraded EVERY correlation to empty.
_CORR_CONCURRENCY = int(os.getenv("BRIEFING_CORRELATION_CONCURRENCY", "3"))
# Per-search timeout (s). The default 10s is calibrated for an idle server; the
# correlation burst runs while the box is already busy.
_CORR_SEARCH_TIMEOUT = float(os.getenv("BRIEFING_CORRELATION_SEARCH_TIMEOUT", "25"))
# A correlated chunk older than this is admitted to an ACTIONABLE thread ONLY if
# its text still names a future date — otherwise a months-old, already-settled
# message (a closed car rental from April) gets fused into today's plan. Looser
# than the 90-day retrieval lookback on purpose: the lookback keeps genuinely
# preparatory chatter ("a chat/mail from a few weeks ago"), this only stops the
# stale tail from posing as a to-do. The exact 2026-06-21/-22 "agence de Cahors"
# bug. Env-tunable.
_CORR_ACTIONABLE_DAYS = int(os.getenv("BRIEFING_CORRELATION_ACTIONABLE_DAYS", "45"))
# Unambiguous French closure phrases: a correlated chunk carrying one describes a
# CLOSED/returned episode, never a to-do — drop it whatever its age. Kept narrow
# and specific on purpose (generic "clôture"/"restitution" can name an UPCOMING
# return, so they are deliberately excluded to avoid false positives). Stored
# accent-stripped + lowercase; matched against the accent-stripped chunk text.
_CORR_CLOSURE_MARKERS = (
    "bienvenue a la maison",
    "merci de votre location",
    "apprecie votre location",
    "dossier clos",
)


def _strip_accents(text: str) -> str:
    """Lowercase + NFKD accent-fold so 'apprécié' matches 'apprecie'."""
    return "".join(
        c for c in unicodedata.normalize("NFKD", text.lower()) if not unicodedata.combining(c)
    )


def _is_stale_correlation(chunk: dict, day: date) -> bool:
    """True when a correlated chunk is closed or too old to be a live to-do.

    Closed → a closure marker in the text (a rental already returned, etc.).
    Too old → the chunk's own date precedes the actionable window AND its text
    names no future date (a weeks-old reservation mail that cites the upcoming
    date survives, judged year-aware by ``nearest_future_date``). Belt and
    braces: the 90-day retrieval lookback stays; this is the actionability gate
    on top of it.
    """
    text = chunk.get("text") or ""
    if any(m in _strip_accents(text) for m in _CORR_CLOSURE_MARKERS):
        return True
    raw = (chunk.get("date_ts") or chunk.get("date") or "")[:10]
    try:
        cdate = date.fromisoformat(raw)
    except ValueError:
        return False  # undated matches are already dropped by the when-label gate
    ref = day.date() if isinstance(day, datetime) else day
    if (ref - cdate).days <= _CORR_ACTIONABLE_DAYS:
        return False
    return nearest_future_date(text, ref) is None


# Decode options for the LOCAL provider (claude-cli ignores them). The vision
# is the run's longest, most writerly reply: the old flat 1024-token default
# cut it off mid-AROUND, and greedy decoding flattened the prose. Its GBNF
# grammar pins the section shape (labels, prose-only MY DAY, sourced AROUND
# bullets) that a 14B otherwise drifts out of. The extractor is the opposite
# — a small JSON pass that wants greedy decoding and a schema grammar.
# 600s wall budget: grammar-constrained sampling over a ~30k-char prompt runs
# 5-6 minutes on an M4 — the 300s default timed the whole vision out.
_VISION_OPTS = {
    "max_tokens": 1800,
    "temperature": 0.3,
    "gbnf_grammar": VISION_GBNF,
    "timeout": 600.0,
    "stage": "vision",
}
_EXTRACTOR_OPTS = {
    "max_tokens": 700,
    "temperature": 0.0,
    "json_schema": EXTRACTOR_JSON_SCHEMA,
    "stage": "extractor",
}

# Surgical READINESS repair: a one-line grammar so the condensed steer can be
# spliced back deterministically. The full repair pass keeps re-dumping the
# WHOOP figures (both v4 attempts did); a focused rewrite of just that line is
# ~25s and cannot disturb the rest of the draft.
_READINESS_REPAIR_GBNF = r"""
root ::= "READINESS: " [^\n]+
"""
_READINESS_REPAIR_OPTS = {
    "max_tokens": 160,
    "temperature": 0.2,
    "gbnf_grammar": _READINESS_REPAIR_GBNF,
    "stage": "readiness_repair",
}


async def _condense_readiness_line(vision_text: str, provider: str, model: str) -> str:
    """Rewrite a figure-dumping READINESS line down to a steer (local only).

    Pure compression of the line's own content — no health data is re-fed, so
    nothing new can be invented. Best-effort: any failure (or a rewrite that
    still recites figures) keeps the original draft untouched.
    """
    span = readiness_line_span(vision_text)
    if not span or not readiness_has_figure_dump(span[2]):
        return vision_text
    start, end, content = span
    prompt = (
        "Rewrite this morning-readiness line as a practical steer for the day. "
        "Keep its language, its advice and its meaning, but name AT MOST ONE "
        "figure — drop the rest of the numbers entirely. One or two sentences, "
        "one single line, starting exactly with 'READINESS: '.\n\n"
        f"READINESS: {content.strip()}"
    )
    try:
        rewritten = await runtime._llm_call(prompt, provider, model, **_READINESS_REPAIR_OPTS)
    except Exception as exc:  # noqa: BLE001 — cosmetic repair, never blocks the briefing
        log.warning("readiness condense failed, keeping original: %r", exc)
        return vision_text
    rewritten = (rewritten or "").strip()
    new_span = readiness_line_span(rewritten)
    if not new_span or readiness_has_figure_dump(new_span[2]):
        log.info("readiness condense: rewrite unusable, keeping original")
        return vision_text
    log.info("readiness condense: %d → %d chars", len(content), len(new_span[2]))
    return vision_text[:start] + rewritten + vision_text[end:]


# Surgical voice repair: the persona tutoies, but a 14B slips into vouvoiement
# and the advisory lint+repair loop ships the best draft anyway. A focused final
# pass rewrites ONLY the address; gemma is already resident (readiness_repair).
_VOICE_REPAIR_OPTS = {
    "max_tokens": 1500,
    "temperature": 0.2,
    "stage": "voice_repair",
}


# The prose defects this pass targets: vouvoiement, coach-speak filler, and
# melodrama. All three are caught by the advisory lint but ship anyway on a 14B;
# the surgical pass below enforces a fix. Naturally empty for a non-French run
# (lint_vision only checks these in French), so the repair self-skips.
_PROSE_DEFECT_TYPES = frozenset({"formal_address", "coach_speak", "melodrama"})


def _prose_defects(text: str) -> int:
    """How many voice/tone defects (vouvoiement, coach-speak, melodrama) remain."""
    return sum(1 for i in lint_vision(text or "", "French") if i.get("type") in _PROSE_DEFECT_TYPES)


async def _repair_voice(vision_text: str, provider: str, model: str) -> str:
    """Enforce tutoiement + sobriety on the day-vision (local only, best-effort).

    Fixes ONLY the address and the tone: vous→tu / votre→ton·ta·tes / ``-ez``
    imperatives→singular, and strips coach-speak filler and dramatization the
    advisory lint flagged but the writer shipped anyway. The rewrite is ACCEPTED
    only if it STRICTLY reduces the defect count without dropping a section label
    or a ``[src: …]`` marker and without truncating — otherwise the original
    draft (no worse than today) is kept. Any failure keeps the original.
    """
    before = _prose_defects(vision_text)
    if not vision_text or before == 0:
        return vision_text
    prompt = (
        "Réécris ce briefing pour corriger SON ADRESSE et SON TON, sans rien "
        "changer d'autre. (1) TUTOIEMENT : « vous » → « tu », « votre/vos » → "
        "« ton/ta/tes », impératifs en « -ez » au singulier (« planifiez » → "
        "« planifie »). (2) SOBRIÉTÉ : retire le remplissage et la "
        "dramatisation (« sans délai », « sans plus attendre », « étape "
        "critique », « impérativement », « pense à »…) et n'ajoute aucune "
        "urgence que les faits ne portent pas. Garde STRICTEMENT à l'identique : "
        "tous les faits, noms propres, chiffres, dates, les étiquettes de début "
        "de ligne (READINESS:, OBJECTIVE:, AROUND:) et chaque marqueur [src: …]. "
        "Ne reformule rien au-delà de ces deux corrections, n'ajoute ni ne "
        "retire aucune information.\n\n"
        f"{vision_text}"
    )
    try:
        rewritten = (
            await runtime._llm_call(prompt, provider, model, **_VOICE_REPAIR_OPTS) or ""
        ).strip()
    except Exception as exc:  # noqa: BLE001 — cosmetic repair, never blocks the briefing
        log.warning("voice repair failed, keeping original: %r", exc)
        return vision_text
    if (
        not rewritten
        or _prose_defects(rewritten) >= before  # no improvement → not worth the risk
        or rewritten.count("[src:") != vision_text.count("[src:")
        or not (0.6 * len(vision_text) <= len(rewritten) <= 1.6 * len(vision_text))
    ):
        log.info("voice repair: rewrite no better, keeping original")
        return vision_text
    for label in ("OBJECTIVE:", "AROUND:", "READINESS:"):
        if label in vision_text and label not in rewritten:
            log.info("voice repair: rewrite dropped %s, keeping original", label)
            return vision_text
    log.info("voice repair: %d → %d voice/tone defect(s)", before, _prose_defects(rewritten))
    return rewritten


_CORR_TOKEN_RE = re.compile(r"[a-zà-ÿ0-9]{4,}", re.IGNORECASE)
# Vocabulary too common to count as a link on its own — an "achat" or a
# "course" shared with a Navigo receipt, a "saint" shared with a wine-fair
# newsletter, is a coincidence, not a correlation.
_CORR_GENERIC_TOKENS = frozenset(
    """
    avec pour chez dans sous vers avant après apres appeler valider acheter
    achat course courses running footing réunion reunion rendez vous point
    daily week weekend cette leur sans plus prévoir prevoir penser faire
    préparer preparer demain matin soir midi aujourd lundi mardi mercredi
    jeudi vendredi samedi dimanche janvier février fevrier mars avril juin
    juillet août aout septembre octobre novembre décembre decembre
    saint sainte stock stocks gare gares lien liens appel appels paris
    """.split()
)


def _distinctive_tokens(text: str) -> set[str]:
    """The anchor's rare vocabulary — the words a real correlation shares."""
    out: set[str] = set()
    for w in _CORR_TOKEN_RE.findall((text or "").lower()):
        if w in _CORR_GENERIC_TOKENS or (w.isdigit() and w.startswith("20") and len(w) == 4):
            continue  # generic words and bare years
        if len(w) >= 5 or any(ch.isdigit() for ch in w):
            out.add(w)
    return out


# A cancellation/postponement cue in a message or article. Word-bounded so
# "annulé" doesn't fire on a substring; covers the French forms plus the
# English one that leaks in from world/world-adjacent sources.
_CANCEL_CUE_RE = re.compile(
    r"\b(annul[ée]?e?s?|annulation|report[ée]?e?s?|cancell?ed|postpon(?:ed|ement))\b",
    re.IGNORECASE,
)


def _flag_cancelled_events(calendar: list[dict], scan_chunks: list[dict]) -> int:
    """Advisory cancellation guard: tag a calendar event ``cancelled=True`` when
    a recent personal/world chunk carries a cancellation cue AND one of the
    event's *distinctive* title tokens.

    Deterministic and never destructive: the event is only flagged, not dropped
    — ``build_registry`` turns the flag into an inline "(annulé …)" note so the
    plan, the writers and the fact-critic all see it, and a cancelled event no
    longer reads as a live pivot. The exact distinctive-token gate is what keeps
    a generic "annulé" in unrelated chatter from mislabelling a real event.
    Returns the number of events flagged (for logging).
    """
    flagged = 0
    for event in calendar:
        title = str(event.get("title") or "")
        tokens = _distinctive_tokens(title)
        if not tokens:
            continue  # no distinctive anchor → can't match safely, leave it live
        for chunk in scan_chunks:
            text = f"{chunk.get('title') or ''} {chunk.get('text') or ''}"
            if not _CANCEL_CUE_RE.search(text):
                continue
            chunk_words = {w[:6] for w in _CORR_TOKEN_RE.findall(text.lower())}
            if any(t[:6] in chunk_words for t in tokens):
                event["cancelled"] = True
                flagged += 1
                break
    return flagged


def _lexical_link(anchor_tokens: set[str], text: str) -> bool:
    """True when ``text`` shares any of the anchor's distinctive vocabulary.

    Prefix-6 matching tolerates inflection and near-spellings (the calendar
    says "cogefrem", the WhatsApp link "cogeferm"). One shared word suffices:
    the token set already excludes generic vocabulary, so a hit means the
    chunk talks about the event's actual subject ("groom")."""
    chunk_prefixes = {w[:6] for w in _CORR_TOKEN_RE.findall((text or "").lower())}
    return any(w[:6] in chunk_prefixes for w in anchor_tokens)


async def _correlate_event(event: dict, after: str = "", day: date | None = None) -> dict | None:
    """Two-arm search for *recent* personal chunks related to one event.

    The query embeds the event title AND its body detail (the description
    often carries the real subject — "achat groom gr200 chez cogefrem" — that
    the bare title lacks). Two complementary retrievals run on it:

    * **dense-only** under the absolute cosine floor (``min_score``) — the
      semantic arm; paraphrases with no shared words.
    * **hybrid**, gated in code by :func:`_lexical_link` — the lexical arm;
      a short title vs a long chat rarely clears the cosine floor even when
      they share "groom"/"cogefrem", and the hybrid ``score`` is a rank, not
      an absolute cosine, so only shared distinctive vocabulary admits here.

    Either signal alone links; generic noise passes neither. ``after`` bounds
    both searches to recent history so a stale-but-similar message can't pose
    as today's plan.
    """
    detail = (event.get("detail") or "").strip()
    base = {
        "corpus": "personal",
        "sources": ["whatsapp", "mail", "notes", "reminders"],
    }
    if after:
        base["after"] = after
    # The dense arm keeps the bare title as its query — a longer query dilutes
    # the embedding and lets brand-adjacent noise clear the floor. The lexical
    # arm queries the distinctive tokens THEMSELVES (BM25 hammers exactly the
    # rare words) and its token gate provides the precision.
    anchor_tokens = _distinctive_tokens(f"{event['title']} {detail}")
    searches = [
        _search_mcp_memory(
            {
                **base,
                "query": event["title"],
                "limit": _CORR_PER_EVENT * 3,
                "min_score": _CORR_MIN_SIMILARITY,
            },
            timeout=_CORR_SEARCH_TIMEOUT,
        )
    ]
    if anchor_tokens:
        searches.append(
            _search_mcp_memory(
                {
                    **base,
                    "query": " ".join(sorted(anchor_tokens)),
                    "limit": _CORR_PER_EVENT * 8,
                },
                timeout=_CORR_SEARCH_TIMEOUT,
            )
        )
    results = await asyncio.gather(*searches)
    dense_results = results[0]
    hybrid_results = results[1] if len(results) > 1 else []
    lexical = [c for c in hybrid_results if _lexical_link(anchor_tokens, (c.get("text") or ""))]
    related: list[dict] = []
    seen_ids: set[str] = set()
    # Lexical matches first: a chunk sharing the event's distinctive
    # vocabulary is the higher-precision link, and the per-event cap is small.
    for chunk in [*lexical, *dense_results]:
        cid = str(chunk.get("id") or id(chunk))
        if cid in seen_ids or not (chunk.get("text") or "").strip():
            continue
        seen_ids.add(cid)
        # /search_memory returns the date under ``date`` (not ``date_ts``);
        # fall back so the model still gets a trustworthy local date, and skip
        # anything we can't date — an undated match can't be claimed as current.
        when = _local_when_label(chunk.get("date_ts") or chunk.get("date"))
        if not when:
            continue
        # An actionable correlation is a live to-do, not a settled episode: drop
        # a closed/months-old chunk before it can be fused into today's plan and
        # certified with an exact [src: …] (the 2026-06-21/-22 "agence de Cahors"
        # bug — a closed April car rental pulled into June's car reminders).
        if day is not None and _is_stale_correlation(chunk, day):
            log.info(
                "correlation: dropped stale/closed chunk %s for event %r",
                cid,
                event.get("title"),
            )
            continue
        chunk["when_label"] = when
        related.append(chunk)
        if len(related) >= _CORR_PER_EVENT:
            break
    if not related:
        return None
    return {"event": event["title"], "when_label": event["when_label"], "chunks": related}


def _parse_event_location(text: str) -> str:
    """Extract the location from a calendar chunk's text.

    Two calendar sources write two shapes:

    * gcal sync writes ``title\\nstart → end\\nlocation\\ndescription`` (room
      codes already stripped), so the location is the line right after the
      ``→`` time line.
    * Apple Calendar writes ``Calendar: … Title: … Start: … End: … Location: …``
      (whitespace-collapsed to a single line by its ingester), so the location
      follows the ``Location:`` label and runs to the end of the text.

    Returns ``""`` when there is none.
    """
    raw = text or ""
    lines = raw.split("\n")
    for i, line in enumerate(lines):
        if "→" in line:
            return lines[i + 1].strip() if i + 1 < len(lines) else ""
    m = re.search(r"\bLocation:\s*(.+?)\s*$", raw)
    return m.group(1).strip() if m else ""


async def _fetch_today_located_events(day: date) -> tuple[list[dict], str]:
    """Today's calendar events with start/end + location, plus the day's
    working location.

    The located events drive travel/weather enrichment; their location is the
    one signal still parsed from the chunk ``text`` (it lives only in Qdrant).
    The working location now rides on the chunk as a structured field, so it is
    read directly — and *before* the start-time filter, so an all-day or
    untimed entry still yields it even when it never enters the located list.
    """
    # window_days=1 + a local-day filter below, NOT window_days=0: the server's
    # window is midnight-anchored, so 0 used to mean "a single instant" and
    # this fetch silently returned [] forever (no chained events, no located
    # weather). The ±1 window works against both the fixed and the old server,
    # and the start-date filter keeps the strip to the briefing day.
    chunks = await _fetch_around_mcp(
        {
            "date": day.isoformat(),
            "window_days": 1,
            "corpus": "personal",
            "sources": list(_CALENDAR_SOURCES),
            "limit": 100,
        }
    )
    events: list[dict] = []
    work_location = ""
    for chunk in chunks:
        # The schedule strip is deterministic coverage, not the actionable
        # to-do list: keep every real calendar event and drop only 'noise'
        # (muted calendars). The tighter _DAY_CALENDAR_GROUP_TYPES set governs
        # the actionable "My day" list and the prose-context filters — using it
        # here silently emptied the strip on a day whose meetings sat on
        # untagged ('unknown') calendars.
        if (chunk.get("group_type") or "unknown") == "noise":
            continue
        work_location = work_location or (chunk.get("working_location") or "")
        start = _parse_iso_datetime(chunk.get("date_ts"))
        if not start:
            continue
        if start.astimezone(LOCAL_TZ).date() != day:
            continue
        end = _parse_iso_datetime(chunk.get("end_date_ts")) or start
        events.append(
            {
                "title": (chunk.get("title") or "").strip(),
                "start": start,
                "end": end,
                # An all-day entry still parses to a midnight datetime, so the
                # renderer can't tell it apart on the timestamps alone: carry the
                # raw-date all-day signal explicitly.
                "all_day": bool(_is_all_day_raw(chunk.get("date"))),
                "location": _parse_event_location(chunk.get("text") or ""),
            }
        )
    events.sort(key=lambda e: e["start"])
    return events, work_location


async def _compute_day_enrichments(day: date, home_location: str) -> dict:
    """Weather for the day + back-to-back event chains (best-effort).

    Weather is keyless (Open-Meteo): it geocodes the home city. The back-to-back
    ``chained`` signal (pure timestamps, no geocoding) carries the
    schedule-pressure cue — it replaced the event→event travel-time transitions
    that were retired with the OpenRouteService integration.
    """
    events, work_location = await _fetch_today_located_events(day)

    # `home_location` already carries the configurable default ("Paris, France")
    # from the `briefing_home_location` setting. If the user explicitly blanked
    # it, geocode_city("") returns None and the weather line is simply omitted —
    # no silent wrong-city fallback.
    weather_coords = await enrichments.geocode_city(home_location)
    weather = await enrichments.weather_for(weather_coords, day) if weather_coords else None

    # Back-to-back chains need no key and no locations — pure timestamps. This
    # is the "the review ends at 17:00 sharp and the leadership sync starts
    # right on it" signal the briefing must surface: the first event feeds
    # straight into the second, so carry its conclusions over.
    chained: list[dict] = []
    for first, second in zip(events, events[1:]):
        if second["start"] <= first["start"]:  # overlapping/all-day noise
            continue
        gap = round((second["start"] - first["end"]).total_seconds() / 60)
        if 0 <= gap <= 10:
            # Titles are untrusted (external invitees) and these lines reach
            # three prompts (vision, plan, thread writer) as stated facts.
            chained.append(
                {
                    "from": sanitize_chunk(first["title"]),
                    "to": sanitize_chunk(second["title"]),
                    "at": second["start"].astimezone(LOCAL_TZ).strftime("%H:%M"),
                    "gap_min": gap,
                }
            )

    return {
        "weather": enrichments.format_weather(weather),
        "chained": chained,
        "work_location": work_location,
        # The located events themselves — the day-load adviser and the
        # code-rendered timeline strip are built from them downstream.
        "events": events,
    }


# Workout sessions live in the user's own notes ("Musculation", "Séance S2…").
# The adviser cites the actual programme, so the steer can say "ta séance du
# carnet passe sur le créneau de midi" instead of a generic "fais du sport".
_WORKOUT_NOTES_QUERY = "séance renforcement musculation entraînement programme workout training"


async def _fetch_workout_notes() -> list[dict]:
    try:
        return await _search_mcp_memory(
            {
                "corpus": "personal",
                "sources": ["notes"],
                "query": _WORKOUT_NOTES_QUERY,
                "limit": 4,
            },
            timeout=20.0,
        )
    except Exception as exc:  # noqa: BLE001 — advice degrades, never blocks
        log.warning("workout-notes search failed: %r", exc)
        return []


async def _generate_day_vision(
    date_str: str,
    actions: dict,
    provider: str,
    model: str,
    news_digest: str = "",
    home_location: str = "",
    extractor_model: str = _HAIKU_MODEL,
    critic_feedback: str = "",
    use_composer: bool = False,
    bestof_n: int = 1,
    budget: TimeBudget | None = None,
) -> tuple[str, dict]:
    """Return ``(vision_text, vision_rows)`` for the day.

    ``news_digest`` (the cross-source news synthesis) is passed in so the
    day-vision becomes the single place world events are related to the
    user's own day — the unifying pass, rather than two stitched sections.

    A cheap structured pre-pass (``extractor_model``) extracts calendar +
    reminder facts in parallel with the other fetches; they are injected into
    the main vision prompt. The pre-pass never blocks: on failure it yields
    safe defaults.

    ``vision_rows`` are the formatted data rows the prompt was built from
    (:func:`prompts._assemble_vision_rows`) — the orchestrator hands them to
    the fact-critic so verification reads exactly what the writer read.
    Returns ``("", {})`` when skipped (no actions) and ``("", rows)`` when the
    LLM call failed after the rows were assembled.
    """
    if not any(actions.values()):
        log.info("day_vision: skipped (no actions)")
        return "", {}

    day = datetime.fromisoformat(date_str).date()
    calendar = actions.get("calendar") or []
    reminders = actions.get("reminders") or []
    (
        wa_chunks,
        context_chunks,
        health_chunks,
        upcoming_events,
        enrichments_data,
        extracted_facts,
        workout_notes,
    ) = await asyncio.gather(
        _fetch_recent_whatsapp(day),
        _fetch_day_context_chunks(day),
        _fetch_health_chunks(day),
        _fetch_upcoming_events(day),
        _compute_day_enrichments(day, home_location),
        extract_day_facts(
            date_str,
            calendar,
            reminders,
            lambda p: runtime._llm_call(p, provider, extractor_model, **_EXTRACTOR_OPTS),
        ),
        _fetch_workout_notes(),
    )
    log.info(
        "day_vision: extractor facts — %d physical, %d partner, %d open loops",
        len(extracted_facts.get("physical_activities") or []),
        len(extracted_facts.get("partner_events") or []),
        len(extracted_facts.get("open_loops") or []),
    )
    if enrichments_data.get("weather") or enrichments_data.get("work_location"):
        log.info(
            "enrichments: weather=%r, work_location=%r",
            enrichments_data.get("weather"),
            enrichments_data.get("work_location"),
        )

    # Reminders are correlation anchors too, not just calendar events: a
    # "settle the shared accounts" reminder has the same cross-source chatter (the
    # WhatsApp thread where the amounts were agreed) an event would. Anchoring only
    # on calendar before meant those threads never got linked. Dedupe by title so a
    # reminder that mirrors an event doesn't search twice.
    anchored_titles = {" ".join(e["title"].lower().split()) for e in upcoming_events}
    for r in actions.get("reminders") or []:
        title = (r.get("title") or "").strip()
        key = " ".join(title.lower().split())
        if not title or key in anchored_titles:
            continue
        anchored_titles.add(key)
        upcoming_events.append(
            {
                "title": title,
                "when_label": _local_when_label(r.get("date_ts")),
                "date_ts": r.get("date_ts") or "",
            }
        )
    upcoming_events = upcoming_events[:_CORR_MAX_EVENTS]

    # Event-anchored correlation: search the personal corpus for chatter related
    # to each near-term event so the vision can connect them deliberately (a
    # flat time-window ranks by recency and crowds the correlated chat out).
    # Bounded to recent history so an old similar message can't pose as today's,
    # and bounded in concurrency so the burst can't time the local server out.
    corr_after = (day - timedelta(days=_CORR_LOOKBACK_DAYS)).isoformat()
    corr_sem = asyncio.Semaphore(_CORR_CONCURRENCY)

    async def _bounded_correlate(e: dict) -> dict | None:
        async with corr_sem:
            return await _correlate_event(e, after=corr_after, day=day)

    correlations = [
        c for c in await asyncio.gather(*[_bounded_correlate(e) for e in upcoming_events]) if c
    ]
    if correlations:
        log.info(
            "event correlations: %d event(s) with related cross-source chunks",
            len(correlations),
        )

    # Stamp each context chunk with its local-time date so the vision reads a
    # trustworthy day instead of doing (error-prone) UTC math on the raw
    # timestamps embedded in item text.
    for chunk in context_chunks:
        chunk["when_label"] = _local_when_label(chunk.get("date_ts"))
    for chunk in health_chunks:
        chunk["when_label"] = _local_when_label(chunk.get("date_ts"))

    # Drop from the generic context any chunk already shown under a correlation
    # cluster, so the same message isn't printed twice in the prompt.
    correlated_ids = {
        str(c.get("id")) for corr in correlations for c in corr["chunks"] if c.get("id")
    }
    if correlated_ids:
        context_chunks = [c for c in context_chunks if str(c.get("id")) not in correlated_ids]

    # The day's working location (home office vs an on-site location) comes from
    # the user's OWN (work-group) calendar events' Google working-location label.
    # Most days share one value; take the most frequent. Falls back to the
    # per-day enrichment value.
    work_locs = [
        (a.get("working_location") or "").strip()
        for a in calendar
        if a.get("group_type") == "work" and (a.get("working_location") or "").strip()
    ]
    work_location = (
        max(set(work_locs), key=work_locs.count)
        if work_locs
        else enrichments_data.get("work_location", "")
    )

    # Cancellation guard (advisory, deterministic): scan the recent personal +
    # world window for a cancellation cue co-occurring with an event's own
    # distinctive title token, and tag the matching calendar events. The flag
    # rides into build_registry as an inline "(annulé …)" note so a cancelled
    # event stops reading as a live pivot and the fact-critic sees the cue too.
    scan_chunks: list[dict] = [*context_chunks, *wa_chunks]
    scan_chunks += [c for corr in correlations for c in corr.get("chunks") or []]
    if news_digest:
        scan_chunks.append({"title": "", "text": news_digest})
    n_cancelled = _flag_cancelled_events(calendar, scan_chunks)
    if n_cancelled:
        log.info("cancellation guard: flagged %d event(s) as cancelled", n_cancelled)

    rows = _assemble_vision_rows(
        date_str,
        calendar,
        reminders,
        wa_chunks,
        context_chunks,
        health_chunks,
        correlations,
        extracted_facts,
        local_mode=provider == "local",
    )

    # Day-load adviser: WHOOP snapshot × the day's computed shape (free slots,
    # meeting span, evening event) × the user's own workout notes → a
    # deterministic recommendation whose FACTS feed the READINESS writer.
    lang_code = "fr" if runtime.language.lower().startswith("fr") else "en"
    located_events = enrichments_data.get("events") or []
    slots = free_slots(located_events, day)
    advice = choose_advice(
        parse_whoop([str(c.get("text") or "") for c in health_chunks]),
        day_features(located_events, slots),
        [
            {"title": str(n.get("title") or ""), "text": str(n.get("text") or "")}
            for n in workout_notes
        ],
        [str(p) for p in (extracted_facts.get("physical_activities") or [])],
        enrichments_data.get("weather") or "",
        lang_code,
    )
    if advice:
        log.info("day_load advice: %s (%d fact(s))", advice["kind"], len(advice["facts"]))

    # Continuity: yesterday's orbit landing on today's calendar opens the
    # narrative ("↩ Hier, le briefing préparait X — c'est aujourd'hui à 15:00").
    day_callbacks = continuity_callbacks(
        load_continuity_state(Path(resolve_data_dir())), date_str, calendar, lang_code
    )
    if day_callbacks:
        log.info("continuity: %d callback(s) from yesterday's briefing", len(day_callbacks))

    # The located events ride on the rows so the orchestrator can render the
    # code-built timeline strip without re-fetching the calendar.
    rows["located_events"] = [
        {
            "title": e["title"],
            "start": e["start"].isoformat(),
            "end": e["end"].isoformat(),
            "all_day": bool(e.get("all_day")),
        }
        for e in located_events
    ]

    if use_composer and provider == "local":
        # Plan-then-write path: the composer's structural guarantees (ID-locked
        # selection, per-thread writers, code-written attributions, per-
        # paragraph verification) replace the mega-prompt for the local model.
        # Any composer failure degrades to the single-pass path below.
        n_rows = sum(len(v) for v in rows.values() if isinstance(v, list))
        log.info("day_vision: composing via plan-then-write (%d data rows)", n_rows)
        try:
            result = await compose_vision(
                date_str,
                rows,
                lambda p, **kw: runtime._llm_call(p, provider, model, **kw),
                day_anchor=_day_anchor(day),
                user_context=runtime.user_context,
                chained=enrichments_data.get("chained") or [],
                news_digest=news_digest,
                critic_feedback=critic_feedback,
                language=runtime.language,
                advice=advice,
                callbacks=day_callbacks,
                bestof_n=bestof_n,
                budget=budget,
            )
            log.info("day_vision: composer returned %d chars", len(result or ""))
            return result, rows
        except ComposerError as exc:
            log.warning("day_vision: composer failed (%s) — falling back to single-pass", exc)
        except Exception as exc:  # noqa: BLE001 — same degradation contract
            log.warning("day_vision: composer crashed (%r) — falling back to single-pass", exc)

    deadline_lines = extract_deadline_lines(rows)
    if deadline_lines:
        log.info("day_vision: %d deadline line(s) mined for the prompt", len(deadline_lines))
    prompt = _build_vision_prompt(
        date_str,
        news_digest=news_digest,
        day_anchor=_day_anchor(day),
        weather=enrichments_data.get("weather", ""),
        chained=enrichments_data.get("chained") or [],
        work_location=work_location,
        extracted_facts=extracted_facts,
        critic_feedback=critic_feedback,
        local_mode=provider == "local",
        rows=rows,
        deadline_lines=deadline_lines,
    )
    log.info("day_vision: calling LLM (%s/%s, prompt=%d chars)", provider, model, len(prompt))
    try:
        result = await runtime._llm_call(prompt, provider, model, **_VISION_OPTS)
        log.info("day_vision: LLM returned %d chars", len(result or ""))
        return result, rows
    except Exception as exc:
        log.warning("Day vision LLM call failed: %r", exc)
        return "", rows
