"""Plan-then-write composer — the local provider's day-vision path.

One mega-prompt asks a 14B to select, correlate, prioritise AND write over
~33k chars at once; it can't. This composer splits the job along the lines a
small model can actually hold, and turns every failure mode the judges found
into something code either prevents or verifies:

1. **Plan** (one JSON call, grammar-locked): every data row carries an ID and
   the schema's ID enum is built from the registry — citing a row that does
   not exist is grammatically impossible. The plan picks 2-3 MY-DAY threads
   (IDs + angle) and the AROUND periphery (ID + stake).
2. **Completeness guard** (code): every today-calendar row missing from the
   threads joins a synthetic "rest of the day" thread — an omitted pivot
   event (the v5 WAFR miss) cannot happen.
3. **Write** (one tiny call per thread): the writer sees ONLY its thread's
   rows — cross-thread fusion is physically impossible, and a ~700-char
   prompt concentrates the model on style instead of retrieval.
4. **Verify per paragraph** (code): the hours and dates a paragraph mentions
   must exist in its own rows; a violation regenerates THAT paragraph once
   (~30s) instead of the whole vision (~5min).
5. **Assemble** (code): AROUND items are filtered (no past, no today-dated
   events posing as periphery, no near-duplicates) and every ``[src: …]``
   attribution is written from the registry — unfalsifiable.

The composed text keeps the READINESS/OBJECTIVE/AROUND contract, so the
renderer, the lints, the critics and the repair loop downstream apply
unchanged. The mega-prompt path remains for cloud providers (and as the
``briefing_composer=single`` kill-switch).
"""

from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable
from datetime import date as _date
from datetime import timedelta as _timedelta
from pathlib import Path

import structlog

from estormi_briefing.compose.continuity import build_state, save_state
from estormi_briefing.compose.exemplars import exemplar_block
from estormi_briefing.lint.fact_lint import (
    extract_date_mentions,
    extract_unit_numbers,
    nearest_future_date,
    normalised_key,
)
from estormi_briefing.lint.vision_lint import lede_issues
from estormi_briefing.llm.bestof import TimeBudget, judge_pick
from memory_core.prompt_templates import render as render_prompt
from memory_core.sanitizer import sanitize_chunk
from memory_core.settings import resolve_data_dir

log = structlog.get_logger()

# Async LLM callable bound to provider/model by the orchestrator; forwards
# the per-call decode options (max_tokens / temperature / json_schema /
# gbnf_grammar / timeout) to ``runtime._llm_call``.
ComposerLlm = Callable[..., Awaitable[str]]

# Single-line paragraph: the writer answers with prose only, no scaffolding.
_PARA_GBNF = r"""
root ::= [^\n] [^\n]+
"""
# Single READINESS line, label included by construction.
_READINESS_GBNF = r"""
root ::= "READINESS: " [^\n]+
"""

# 1100: at 700 the plan JSON for a full registry (3 threads + 6 French stakes)
# truncated mid-object — the grammar×truncation trap, on the plan call.
_PLAN_OPTS = {"max_tokens": 1100, "temperature": 0.1, "timeout": 480.0, "stage": "plan"}
_WRITER_OPTS = {
    "max_tokens": 220,
    "temperature": 0.35,
    "gbnf_grammar": _PARA_GBNF,
    "timeout": 240.0,
    "stage": "writer",
}
_READINESS_OPTS = {
    "max_tokens": 140,
    "temperature": 0.2,
    "gbnf_grammar": _READINESS_GBNF,
    "timeout": 180.0,
    "stage": "readiness",
}
# The lede gets a little temperature on purpose: best-of-N needs N *different*
# candidates, and a greedy 14B would emit the same sentence three times.
_LEDE_OPTS = {
    "max_tokens": 120,
    "temperature": 0.45,
    "gbnf_grammar": _PARA_GBNF,
    "timeout": 180.0,
    "stage": "lede",
}

_MAX_THREADS = 3
# Code-written angle (never user-facing): the replacement for a
# news-contaminated plan angle — a writer instruction, not briefing content.
_NEUTRAL_ANGLE = "Énonce chaque fait sobrement — sa date, son heure, rien d'autre."
# Title-prefix length for "is this event already covered" matching: long
# enough to be specific, short enough to survive truncated titles.
_TITLE_MATCH_PREFIX = 24
# Durations read like clock times ("une session de 2h") — exclude the usual
# duration prepositions so only schedule times trip the verification.
_HOURS_RE = re.compile(
    r"(?<!de )(?<!durant )(?<!pendant )\b(\d{1,2})\s*[h:]\s*(\d{2})\b"
    r"|(?<!de )(?<!durant )(?<!pendant )\b(\d{1,2})\s*h\b",
    re.IGNORECASE,
)
# French clock times spelt out in words — the channel a 14B uses to slip past
# the numeric containment ("À midi trente" for a 13:30 quiz, in production).
# Bare "midi"/"minuit" stay exempt (idiomatic: "le créneau du midi"), but a
# word-hour WITH minutes or the "X heures (trente)" form asserts a schedule.
_WORD_NUMS = {
    "une": 1,
    "deux": 2,
    "trois": 3,
    "quatre": 4,
    "cinq": 5,
    "six": 6,
    "sept": 7,
    "huit": 8,
    "neuf": 9,
    "dix": 10,
    "onze": 11,
    "douze": 12,
    "treize": 13,
    "quatorze": 14,
    "quinze": 15,
    "seize": 16,
    "dix-sept": 17,
    "dix-huit": 18,
    "dix-neuf": 19,
    "vingt": 20,
    "vingt-et-une": 21,
    "vingt et une": 21,
    "vingt-deux": 22,
    "vingt-trois": 23,
}
_WORD_MINUTES = {
    "cinq": 5,
    "dix": 10,
    "quinze": 15,
    "et quart": 15,
    "vingt": 20,
    "trente": 30,
    "et demie": 30,
    "et demi": 30,
    "quarante": 40,
    "quarante-cinq": 45,
    "cinquante": 50,
}
_WORD_HOUR_RE = re.compile(
    r"(?<!de )(?<!durant )(?<!pendant )"
    r"(?<![-\w])(midi|minuit|(?:une|deux|trois|quatre|cinq|six|sept|huit|neuf"
    r"|dix-sept|dix-huit|dix-neuf|dix|onze|douze|treize|quatorze|quinze|seize"
    r"|vingt(?:-et-une|-deux|-trois| et une)?)\s+heures?)"
    r"(?:\s+(cinq|dix|quinze|et quart|vingt|trente|et demie?|quarante-cinq"
    r"|quarante|cinquante))?\b",
    re.IGNORECASE,
)


# ── registry ──────────────────────────────────────────────────────────────────

# Citation labels the user reads. Chunk sources arrive under several slugs for
# the same real-world source (Apple Calendar chunks are `calendar`, Google's
# are `gcal`, the day's own rows are code-labelled `agenda`) — one event cited
# via two routes must never read as two different sources in a [src: …] marker.
_DISPLAY_LABELS = {"calendar": "agenda", "gcal": "agenda", "reminders": "reminder"}


def _display_label(source: str) -> str:
    return _DISPLAY_LABELS.get(source, source)


def _iso_day(*candidates: str) -> str:
    """First candidate whose 10-char prefix is a real ISO day, else ``""``.

    Graph rows carry bare clock times in ``when_label`` ("09:45") — sliced
    blindly those used to masquerade as entry dates all the way into the
    rendered attribution."""
    for c in candidates:
        c = (c or "")[:10]
        try:
            _date.fromisoformat(c)
        except ValueError:
            continue
        return c
    return ""


def build_registry(rows: dict, date_str: str) -> list[dict]:
    """Flatten the vision rows into ID'd entries the plan selects from.

    Each entry: ``{id, kind, label, when, date, deadline, deadline_iso, text}``
    — ``when`` is the human form shown to the model ("aujourd'hui 10:00", a
    when_label…), ``date`` the ISO day when known (calendar/reminders = the
    briefing day), ``label``/``when`` also feed the code-written ``[src: …]``
    attributions. ``deadline``/``deadline_iso`` carry the next future date the
    row's own text names — mined in code so even a weak model gets the
    actionable angle of a fact (the June cutoff, not the 2027 headline).
    """
    reg: list[dict] = []
    counters: dict[str, int] = {}
    try:
        today = _date.fromisoformat(date_str[:10])
    except ValueError:
        today = None

    def add(kind: str, label: str, when: str, date_iso: str, text: str, title: str = "") -> None:
        counters[kind] = counters.get(kind, 0) + 1
        text = " ".join((text or "").split())[:300]
        deadline_iso = ""
        deadline = ""
        if today is not None:
            nd = nearest_future_date(text, today)
            # Annotate only a deadline beyond the row's own date — restating
            # the entry's date (or the briefing day) as an "échéance" is noise.
            if nd and nd.isoformat() not in (date_str[:10], date_iso):
                deadline_iso = nd.isoformat()
                deadline = _human_date(deadline_iso)
        reg.append(
            {
                "id": f"{kind}{counters[kind]}",
                "kind": kind,
                "label": label,
                "when": when,
                "date": date_iso,
                "deadline": deadline,
                "deadline_iso": deadline_iso,
                "title": " ".join((title or "").split())[:120],
                "text": text,
            }
        )

    for a in rows.get("calendar") or []:
        when = a.get("when") or "toute la journée"
        # Structured event flags ride into the row text so the plan, the
        # writers AND the lede see them — the cloud mega-prompt has carried
        # them since day one, but this path silently dropped the "maybe" RSVP
        # (the bench's quiz was tentative; only the cloud versions knew).
        flags = ""
        if a.get("tentative"):
            flags += " (tentative — « peut-être », présence non confirmée)"
        if a.get("cancelled"):
            # The cancellation guard (day_vision._flag_cancelled_events) found a
            # cancellation cue naming this event in the recent window. Flag it
            # inline so the plan, the writers and the fact-critic all treat it
            # as cancelled — a cancelled event must never anchor the day.
            flags += " (ANNULÉ — événement annulé/reporté, ne pas en faire le pivot du jour)"
        if a.get("event_type") == "outOfOffice":
            flags += " (absence — out of office, pas une réunion)"
        elif a.get("event_type") == "focusTime":
            flags += " (focus time — bloc de travail auto-réservé, pas une réunion)"
        add(
            "A",
            "agenda",
            f"aujourd'hui {when}",
            date_str,
            f"[{a.get('group_type') or 'unknown'}] {a.get('title')}{flags}",
        )
    for r in rows.get("overdue") or []:
        add("R", "reminder", "EN RETARD", "", r.get("title") or "")
    for r in rows.get("today_rem") or []:
        add(
            "R",
            "reminder",
            f"aujourd'hui {r.get('when') or ''}".strip(),
            date_str,
            r.get("title") or "",
        )
    for t in rows.get("threads") or []:
        for row in t.get("rows") or []:
            add(
                "T",
                _display_label(str(row.get("source") or "context")),
                row.get("when_label") or "",
                _iso_day(row.get("date") or "", row.get("when_label") or ""),
                f"(fil:{t.get('anchor')}) {row.get('title')}: {row.get('text')}",
                title=str(row.get("title") or ""),
            )
    for c in rows.get("corr_blocks") or []:
        for row in c.get("rows") or []:
            add(
                "L",
                _display_label(str(row.get("source") or "context")),
                row.get("when_label") or "",
                _iso_day(row.get("date") or "", row.get("when_label") or ""),
                f"(lié à: {c.get('event')}) {row.get('title')}: {row.get('text')}",
                title=str(row.get("title") or ""),
            )
    for c in rows.get("ctx_rows") or []:
        add(
            "C",
            _display_label(str(c.get("source") or "context")),
            c.get("when_label") or "",
            _iso_day(c.get("date") or "", c.get("when_label") or ""),
            f"{c.get('title')}: {c.get('text')}",
            title=str(c.get("title") or ""),
        )
    for b in rows.get("wa_blocks") or []:
        # Conversation labels are contact/chat names — untrusted external
        # text like everything else, and they end up in the prompt AND the
        # code-written [src: …] attribution.
        label = sanitize_chunk(str(b.get("label") or "").split(" [")[0])
        add("W", f"WhatsApp · {label}", "", "", " / ".join(b.get("texts") or []))
    return reg


def add_news_entries(registry: list[dict], news_digest: str, limit: int = 6) -> None:
    """Append the day's news bullets as ``N`` entries (periphery candidates).

    The mega-prompt wove world news into the day; the composer keeps that
    value by letting the plan pick at most a couple of news rows for AROUND
    (they are filtered out of MY DAY threads by kind downstream)."""
    n = 0
    for line in (news_digest or "").splitlines():
        line = line.strip()
        if not line.startswith(("- ", "• ")):
            continue
        n += 1
        # The code-attached [SOURCE: …] marker is attribution plumbing, not
        # content — and its labels would otherwise count as news-only proper
        # nouns in the fusion guard below.
        text = re.sub(r"\s*\[SOURCE:[^\]]*\]", "", line[2:])
        registry.append(
            {
                "id": f"N{n}",
                "kind": "N",
                "label": "news",
                "when": "",
                "date": "",
                "deadline": "",
                "deadline_iso": "",
                "title": "",
                "text": " ".join(text.split())[:300],
            }
        )
        if n >= limit:
            return


def plan_schema(ids: list[str]) -> dict:
    """Plan JSON schema with the registry IDs as an enum — a hallucinated row
    reference is impossible under the grammar.

    No free-text "objective" field any more: the plan model's mission-statement
    ledes were the briefing's worst sentence, so the opening line is now written
    by :func:`_write_lede` from the pivot thread's own facts."""
    id_enum = {"type": "string", "enum": ids or ["NONE"]}
    return {
        "type": "object",
        "properties": {
            "myday_threads": {
                "type": "array",
                "minItems": 1,
                "maxItems": _MAX_THREADS,
                "items": {
                    "type": "object",
                    "properties": {
                        "ids": {"type": "array", "minItems": 1, "maxItems": 5, "items": id_enum},
                        "angle": {"type": "string"},
                    },
                    "required": ["ids", "angle"],
                },
            },
            "around": {
                "type": "array",
                "minItems": 0,
                "maxItems": 6,
                "items": {
                    "type": "object",
                    "properties": {"id": id_enum, "stake": {"type": "string"}},
                    "required": ["id", "stake"],
                },
            },
        },
        "required": ["myday_threads", "around"],
    }


# ── deterministic verification ────────────────────────────────────────────────


def _hours_in(text: str) -> set[str]:
    """Normalised clock times mentioned in ``text`` — "10h", "9h45", "17:00",
    and the spelt-out French forms ("quatorze heures", "midi trente")."""
    out: set[str] = set()
    for m in _HOURS_RE.finditer(text or ""):
        if m.group(3) is not None:
            out.add(f"{int(m.group(3))}:00")
        else:
            out.add(f"{int(m.group(1))}:{m.group(2)}")
    for m in _WORD_HOUR_RE.finditer(text or ""):
        head = m.group(1).lower()
        minutes_word = (m.group(2) or "").lower()
        if head == "midi":
            hour = 12
        elif head == "minuit":
            hour = 0
        else:
            hour = _WORD_NUMS.get(head.removesuffix(" heures").removesuffix(" heure"), -1)
        if hour < 0:
            continue
        if head in ("midi", "minuit") and not minutes_word:
            continue  # bare "midi"/"minuit" is idiom, not a schedule assertion
        minute = _WORD_MINUTES.get(minutes_word, 0) if minutes_word else 0
        out.add(f"{hour}:{minute:02d}")
    return out


def paragraph_violations(paragraph: str, entries: list[dict], date_str: str) -> list[str]:
    """Hours/dates a paragraph asserts that its own rows don't contain.

    The writer only saw ``entries`` — any other time or date is invented.
    The briefing day and the next two are always allowed (the day anchor
    names them, so "demain, le 12 juin" is legitimate prose). Returns
    human-readable violation strings (empty = clean).
    """
    allowed_hours: set[str] = set()
    allowed_dates: set[tuple[int, int]] = set()
    source_text = " ".join(f"{e['when']} {e['text']}" for e in entries)
    allowed_hours |= _hours_in(source_text)
    for _, candidates in extract_date_mentions(source_text):
        allowed_dates |= candidates
    try:
        d = _date.fromisoformat(date_str[:10])
        for off in range(3):
            nd = d + _timedelta(days=off)
            allowed_dates.add((nd.month, nd.day))
    except ValueError:
        pass

    violations: list[str] = []
    for hour in _hours_in(paragraph) - allowed_hours:
        violations.append(f"l'heure {hour} n'apparaît dans aucun fait du fil")
    for context, candidates in extract_date_mentions(paragraph):
        if allowed_dates and not (candidates & allowed_dates):
            violations.append(f"la date dans « {context.strip()[:60]} » n'est pas dans les faits")
    return violations


def _entry_is_past(entry: dict, date_str: str) -> bool:
    """True when the entry is genuinely stale periphery.

    The row date is the CHUNK's date (when the mail/message arrived), not the
    subject's: a March mail about a June deadline must survive. An entry is
    past only when its chunk date precedes the briefing day AND its text
    names no future date — judged year-aware by ``nearest_future_date``
    (a year-less "5 janvier" stays alive across New Year; an explicit
    "28 mai 2026" already behind us does NOT resurrect as 2027's).
    """
    d = (entry.get("date") or "")[:10]
    if not d or d >= date_str[:10]:
        return False
    try:
        today = _date.fromisoformat(date_str[:10])
    except ValueError:
        return False
    return nearest_future_date(entry.get("text") or "", today) is None


def _norm_key(text: str) -> str:
    """Full-length normalisation (blobs and keys alike) — see fact_lint."""
    return normalised_key(text, limit=None)


def _event_title(entry_text: str) -> str:
    """The bare event title from a calendar entry text ("[work] ADR …")."""
    return re.sub(r"^\[[^\]]*\]\s*", "", entry_text or "")


_MONTH_SHORT = [
    "",
    "Jan",
    "Feb",
    "Mar",
    "Apr",
    "May",
    "Jun",
    "Jul",
    "Aug",
    "Sep",
    "Oct",
    "Nov",
    "Dec",
]


def _human_date(iso: str) -> str:
    """ "2026-06-12" → "12 Jun" — the attribution form the renderer expects.

    Non-dates return ``""`` (never the input): a graph row's bare clock time
    used to slip through here and render as ``calendar · 09:45`` — a time
    posing as a date in the attribution."""
    try:
        d = _date.fromisoformat(iso[:10])
    except ValueError:
        return ""
    return f"{d.day} {_MONTH_SHORT[d.month]}"


# The correlation-anchor prefix a registry entry's text can carry — "(fil: …)"
# for a thread row, "(lié à: …)" for a linked chunk. The SAME chunk can enter
# the registry under both prefixes (once anchored to its thread, once linked to
# an event), so it must be stripped before any dedup key or fact fallback reads
# the bare text — otherwise the two prefixes look like two distinct rows.
_ANCHOR_PREFIX_RE = re.compile(r"^\((?:fil|lié à):[^)]*\)\s*")

# ISO tokens inside raw chunk text ("2026-06-12T16:00:00+02:00", bare
# "2026-06-13") — humanised by the stake fallback so machine timestamps never
# reach the reader.
_ISO_DT_RE = re.compile(r"\b(\d{4}-\d{2}-\d{2})T(\d{2}):(\d{2})(?::\d{2})?(?:[+-]\d{2}:\d{2}|Z)?")
_ISO_BARE_RE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")


_SEGMENT_SPLIT_RE = re.compile(r"(?<=[.!?])\s+|\s+[–—•]\s+|\s{2,}")


def _deadline_segment(text: str, deadline_iso: str) -> str:
    """The text segment that carries the entry's next deadline, if any.

    A long chunk's load-bearing fact is its future date ("Pique-nique :
    14 juin 2026" buried in a past meeting's agenda) — surface THAT segment
    instead of the chunk's opening words."""
    try:
        d = _date.fromisoformat(deadline_iso)
    except ValueError:
        return ""
    for segment in _SEGMENT_SPLIT_RE.split(text or ""):
        segment = segment.strip()
        if not (3 <= len(segment) <= 120):
            continue
        for _, candidates in extract_date_mentions(segment):
            if (d.month, d.day) in candidates:
                return segment
    return ""


# Invisible characters mail clients smuggle into subjects/preheaders: the
# object-replacement char (a stripped inline image), zero-widths, BOM. They
# render as "￼" boxes in the shipped briefing.
_INVISIBLE_RE = re.compile(r"[￼​‌‍⁠﻿]")
# Mail greeting/preheader boilerplate riding behind the subject ("MY CONSOLE
# Hi Alex, You're receiving…") — everything from a greeting token on is
# body noise, never the fact.
_MAIL_GREETING_RE = re.compile(
    r"\s*\b(?:Hi|Hello|Dear|Bonjour|Salut|Cher|Chère)\b\s+[A-ZÀ-Þ][\w'’-]*\s*,.*$",
    re.DOTALL,
)


def _is_french(language: str) -> bool:
    return (language or "").strip().lower().startswith("fr")


def _fact_fallback(entry: dict, language: str = "French") -> str:
    """Code-rendered stake: the row's own fact, cleaned for display.

    Used whenever the model's stake is rejected (invented deadline, fabricated
    relation, news leak, editorialising) — the raw chunk text it replaces
    carried machine timestamps, mail headers and reminder boilerplate straight
    into the briefing."""
    text = _ANCHOR_PREFIX_RE.sub("", entry.get("text") or "").strip()
    text = _INVISIBLE_RE.sub(" ", text)
    title = _INVISIBLE_RE.sub(" ", (entry.get("title") or "")).strip()
    # A mail subject with the preheader glued on ("Migrate … ￼ MY CONSOLE Hi
    # Alex, You're r") — cut at the greeting; the subject is the fact.
    title = _MAIL_GREETING_RE.sub("", title).strip()
    if len(title) > 90:
        title = title[:90].rsplit(" ", 1)[0].rstrip(" ,;:—-") + "…"
    # Mail rows: the subject (= row title) is the fact; the body tail is
    # noise. When the row carries a future deadline buried in the body, that
    # segment IS the load-bearing part — render "subject — segment".
    if entry.get("label") == "mail" and title:
        segment = _deadline_segment(text, entry.get("deadline_iso") or "")
        if segment and segment.lower().startswith(title.lower()[:24]):
            text = segment  # the segment already opens on the subject
        elif segment and segment not in title:
            text = f"{title} — {segment}"
        else:
            text = title
    text = re.sub(r"^\[[^\]]{2,40}\]\s*", "", text)  # "[Action Advised]" tag prefixes
    # Mail copyright footers riding behind the subject ("…: © Coinbase 2026 |
    # Coinbase Luxembourg S.A.") — never the fact.
    text = re.sub(r"\s*[:—–-]?\s*©.*$", "", text)
    # WhatsApp sender handles ("[100000000000004@lid]:") are routing metadata,
    # and a raw URL is unclickable noise in a one-line stake — name it instead.
    text = re.sub(r"\[[^\]\s]*@(?:lid|s\.whatsapp\.net|g\.us)\]\s*:?\s*", "", text)
    text = re.sub(r"https?://\S+", "(lien)" if _is_french(language) else "(link)", text)
    # Reminder chunks: "List: X Title: … Due: … Status: pending" → the title + due.
    text = re.sub(r"\bList:\s*\S+\s+Title:\s*", "", text)
    text = re.sub(r"\s*\bStatus:\s*(?:pending|completed)\b", "", text)
    text = re.sub(r"\bDue:\s*", "échéance " if _is_french(language) else "due ", text)
    text = _ISO_DT_RE.sub(
        lambda m: f"{_human_date(m.group(1))} {int(m.group(2))}h{m.group(3)}", text
    )
    text = _ISO_BARE_RE.sub(lambda m: _human_date(m.group(0)) or m.group(0), text)
    # "TCL Time : TCL Time …" — chunk text restating its own title. The
    # lookahead (not \b) tolerates titles ending in a paren or punctuation;
    # the lazy group tolerates a trailing space before the colon.
    text = re.sub(r"^([^:]{3,60}?)\s*:\s*\1(?=\s|$)\s*", r"\1 — ", text)
    text = " ".join(text.split())
    if len(text) > 140:
        # Cut at a word boundary — a mid-word chop ("Migrate Fireb") reads
        # like model garbage even when it's code-rendered truth.
        text = text[:140].rsplit(" ", 1)[0] + "…"
    return text.strip(" -—·:")


def filter_around(
    plan_around: list[dict], by_id: dict, date_str: str, language: str = "French"
) -> list[dict]:
    """Code-side periphery filter: drop unknown IDs, past items, today's
    calendar events posing as periphery, and near-duplicates. A stake whose
    hours/dates aren't in its own row is replaced by the code-rendered fact —
    the model never gets to decorate a fact with an invented deadline."""
    out: list[dict] = []
    seen: set[str] = set()
    for item in plan_around or []:
        entry = by_id.get(item.get("id") or "")
        if entry is None:
            continue
        if _entry_is_past(entry, date_str):
            continue
        # Today's own calendar and due-today/overdue reminders never orbit —
        # presenting today's action as periphery is the v5 inversion.
        if entry["kind"] in ("A", "R"):
            continue
        # The same inversion in disguise: a today-dated calendar chunk riding
        # in through a graph/context row (normalised label "agenda").
        if entry["label"] == "agenda" and (entry.get("date") or "")[:10] == date_str[:10]:
            continue
        # Strip the correlation-anchor prefix before keying: the same chunk can
        # orbit under "(fil: …)" and "(lié à: …)" — two prefixes, one fact — and
        # must render once, not twice (mirrors the seen-set in
        # build_daily_note._render_around_html).
        key = _norm_key(_ANCHOR_PREFIX_RE.sub("", entry["text"])) or _norm_key(
            item.get("stake") or ""
        )
        if key in seen:
            continue
        seen.add(key)
        stake = (item.get("stake") or "").strip()
        if stake and paragraph_violations(stake, [entry], date_str):
            stake = _fact_fallback(entry, language)
        out.append({**item, "stake": stake, "entry": entry})
    return out


def _src_marker(entries: list[dict]) -> str:
    """The code-written ``[src: …]`` for a thread — real label, real date,
    never the model's word for it. One marker per distinct LABEL (max two),
    dates humanised ("12 Jun") to match the renderer's attribution style."""
    seen_labels: list[str] = []
    parts: list[str] = []
    for e in entries:
        if e["label"] in seen_labels:
            continue
        seen_labels.append(e["label"])
        when = _human_date(e["date"]) if e.get("date") else ""
        parts.append(f"{e['label']}{' · ' + when if when else ''}")
        if len(parts) == 2:
            break
    return f"[src: {' + '.join(parts)}]" if parts else ""


# ── fusion guards ─────────────────────────────────────────────────────────────
# The hour/date verification leaves three free-text channels open (objective,
# angle, stake) — exactly where a 14B confabulates relations: world news woven
# into a 15-minute standup, "to confirm after <unrelated event>". These guards
# are deterministic: proper nouns that exist ONLY in the news rows are the
# fusion signature, and a stake naming ANOTHER row's event is a fabricated
# relation.

_PROPER_NOUN_RE = re.compile(r"\b[A-ZÀ-Þ][A-Za-zà-ÿÀ-Þ'’\-]{2,}\b")
# Characters that end a sentence/bullet — a capitalised word right after one
# is ordinary sentence case, not a proper noun.
_SENTENCE_BREAKERS = '.!?:;•·–—-"«»()[]'


def _proper_nouns(text: str) -> set[str]:
    """Capitalised tokens in non-sentence-initial position, lowercased."""
    out: set[str] = set()
    for m in _PROPER_NOUN_RE.finditer(text or ""):
        head = text[: m.start()].rstrip()
        if not head or head[-1] in _SENTENCE_BREAKERS:
            continue
        out.add(m.group(0).lower())
    return out


def news_only_nouns(registry: list[dict], trusted_text: str = "") -> set[str]:
    """Proper nouns appearing in news rows but in NO personal row (nor in the
    trusted context: user profile, day anchor). Their presence in MY-DAY prose
    or an AROUND stake can only come from world news — the personal/world
    fusion the composer exists to prevent."""
    news_text = " ".join(e["text"] for e in registry if e["kind"] == "N")
    own = (
        " ".join(e["text"] for e in registry if e["kind"] != "N") + " " + (trusted_text or "")
    ).lower()
    return {n for n in _proper_nouns(news_text) if n not in own}


def _noun_leaks(text: str, foreign: set[str], own_text: str = "") -> list[str]:
    """The ``foreign`` nouns present in ``text`` but absent from its own facts."""
    low = (text or "").lower()
    own = (own_text or "").lower()
    return sorted(n for n in foreign if n not in own and re.search(rf"\b{re.escape(n)}\b", low))


def _other_entry_titles(registry: list[dict]) -> list[tuple[str, str]]:
    """(normalised title, entry id) pairs usable for cross-item contamination
    checks — long enough to be specific, news rows excluded (they have no
    event title)."""
    out: list[tuple[str, str]] = []
    for e in registry:
        if e["kind"] == "N":
            continue
        text = _ANCHOR_PREFIX_RE.sub("", e["text"])
        title = _event_title(text).split(":", 1)[0]
        norm = _norm_key(title)
        if len(norm) >= 6:
            out.append((norm, e["id"]))
    return out


def _stake_contaminations(stake: str, entry: dict, titles: list[tuple[str, str]]) -> list[str]:
    """Other rows' event titles that ``stake`` names — fabricated relations.

    A stake may only speak about its own row; "bar à jeux … à confirmer après
    le TCL Time" invents a dependency between two unrelated registry rows."""
    norm_stake = _norm_key(stake)
    own = _norm_key(entry.get("text") or "")
    return [t for t, eid in titles if eid != entry["id"] and t not in own and t in norm_stake]


_WORD_RE = re.compile(r"[a-zà-ÿ]+", re.IGNORECASE)
# Words a stake may always use without tracing back to its row: French
# function words (4+ letters — shorter ones pass the length gate), the
# temporal lexicon (the plan is asked to restate each fact WITH its date),
# and neutral restating glue. Deliberately small: anything not here and not
# in the row is treated as editorialising.
_STAKE_FREE_WORDS = frozenset(
    """
    avec dans sans chez vers sous entre pour cette celui celle leur leurs
    elles vous nous tout toute tous toutes être sont sera seront était avoir
    aura fait faire doit doivent faut ainsi alors aussi autre autres bien
    comme donc encore enfin ensuite mais même mêmes moins plus peut peuvent
    quand selon puis votre notre deux trois quatre cinq sept huit neuf
    lundi mardi mercredi jeudi vendredi samedi dimanche
    janvier février fevrier mars avril juin juillet août aout septembre
    octobre novembre décembre decembre
    demain hier semaine weekend matin soir midi après apres avant prochain
    prochaine jusqu échéance echeance prévu prévue prévus prévues prevu
    prevue attendu attendue rappel reste restent
    """.split()
)


def _stake_unsupported_words(stake: str, entry: dict) -> list[str]:
    """Content words in ``stake`` that its own row does not contain.

    Radical by design: a stake is a restatement of ONE fact, so every word of
    4+ letters must trace back to the row (a 6-letter prefix match tolerates
    French inflection — "réservation"/"réserver"), to the temporal lexicon, or
    to the mined deadline. Anything else is editorialising — the channel every
    residual confabulation used ("liste de tâches à valider", "mise à jour
    technique des systèmes domestiques")."""
    own = " ".join(str(entry.get(k) or "") for k in ("text", "when", "deadline", "label")).lower()
    own_words = set(_WORD_RE.findall(own))
    own_prefixes = {w[:6] for w in own_words if len(w) >= 6}
    out = set()
    for w in (m.lower() for m in _WORD_RE.findall(stake or "")):
        if len(w) < 4 or w in _STAKE_FREE_WORDS or w in own_words:
            continue
        if len(w) >= 6 and w[:6] in own_prefixes:
            continue
        out.add(w)
    return sorted(out)


# ── periphery decay ───────────────────────────────────────────────────────────
# A periphery fact with a far-off deadline (the "Firebase shuts down in 2027"
# mail) re-orbits every single day until that deadline. After it has been shown
# twice it goes quiet, resurfacing only when its deadline comes within a week.

_AROUND_SEEN_FILE = "briefing_around_seen.json"
_AROUND_MAX_SHOWINGS = 2
_AROUND_DEADLINE_WINDOW_DAYS = 7
_AROUND_SEEN_RETENTION_DAYS = 60


def _around_seen_path() -> Path:
    # Resolved per call (not at import) so the test suite's ESTORMI_DATA_DIR
    # override and the bundle's env always win.
    return Path(resolve_data_dir()) / _AROUND_SEEN_FILE


def _decay_seen_around(items: list[dict], date_str: str) -> list[dict]:
    """Drop periphery already shown ``_AROUND_MAX_SHOWINGS`` times unless its
    deadline is imminent; record today's showings. Same-day re-runs (critic
    retries, manual relaunches) are idempotent. Never raises."""
    try:
        today = _date.fromisoformat(date_str[:10])
    except ValueError:
        return items
    seen_path = _around_seen_path()
    try:
        state = json.loads(seen_path.read_text())
        if not isinstance(state, dict):
            state = {}
    except Exception:  # noqa: BLE001 — missing/corrupt state never blocks
        state = {}
    kept: list[dict] = []
    for item in items:
        e = item["entry"]
        key = _norm_key(e["text"])
        info = state.get(key) if isinstance(state.get(key), dict) else {}
        shown = int(info.get("count") or 0)
        shown_today = info.get("last") == today.isoformat()
        imminent = False
        if e.get("deadline_iso"):
            try:
                left = (_date.fromisoformat(e["deadline_iso"]) - today).days
                imminent = 0 <= left <= _AROUND_DEADLINE_WINDOW_DAYS
            except ValueError:
                pass
        if shown >= _AROUND_MAX_SHOWINGS and not shown_today and not imminent:
            log.info("composer: periphery retired after %d showings: %.60s", shown, e["text"])
            continue
        kept.append(item)
        state[key] = {
            "count": shown if shown_today else shown + 1,
            "last": today.isoformat(),
        }
    try:
        horizon = (today - _timedelta(days=_AROUND_SEEN_RETENTION_DAYS)).isoformat()
        state = {k: v for k, v in state.items() if str(v.get("last") or "") >= horizon}
        seen_path.parent.mkdir(parents=True, exist_ok=True)
        seen_path.write_text(json.dumps(state, ensure_ascii=False))
    except Exception:  # noqa: BLE001 — persistence is best-effort
        log.warning("composer: around-seen state not persisted")
    return kept


# ── composition ───────────────────────────────────────────────────────────────


async def compose_vision(
    date_str: str,
    rows: dict,
    llm: ComposerLlm,
    *,
    day_anchor: str = "",
    user_context: str = "",
    chained: list[dict] | None = None,
    news_digest: str = "",
    critic_feedback: str = "",
    language: str = "French",
    advice: dict | None = None,
    callbacks: list[str] | None = None,
    bestof_n: int = 1,
    budget: TimeBudget | None = None,
) -> str:
    """Compose the day-vision via plan → write → verify → assemble.

    Any stage failure raises :class:`ComposerError` so the caller can degrade
    to the single-pass path.
    """
    registry = build_registry(rows, date_str)
    if not registry:
        raise ComposerError("empty registry")
    add_news_entries(registry, news_digest)
    by_id = {e["id"]: e for e in registry}
    # Proper nouns that only the news rows contain — any of them surfacing in
    # an objective/angle/stake/paragraph is world content leaking into the
    # personal narrative (the profile and day anchor are trusted vocabulary).
    foreign_nouns = news_only_nouns(registry, trusted_text=f"{user_context} {day_anchor}")

    plan_raw = await llm(
        render_prompt(
            "briefing_plan",
            date_str=date_str,
            day_anchor=day_anchor,
            user_context=user_context,
            registry=registry,
            chained=chained or [],
            critic_feedback=critic_feedback,
            language=language,
        ),
        json_schema=plan_schema([e["id"] for e in registry]),
        **_PLAN_OPTS,
    )
    try:
        plan = json.loads(plan_raw)
    except json.JSONDecodeError as exc:  # grammar makes this near-impossible
        raise ComposerError(f"plan unparseable: {exc}") from exc

    threads = [t for t in plan.get("myday_threads") or [] if t.get("ids")][:_MAX_THREADS]
    if not threads:
        raise ComposerError("plan produced no threads")

    # No completeness threads any more — the inversion. Coverage of the bare
    # schedule now belongs to CODE (the timeline strip + the reminders line in
    # build_daily_note), so the prose carries only what the calendar cannot
    # say: real threads with a stake. The old guard forced every uncovered
    # event into a synthetic "rest of the day" paragraph — the single biggest
    # source of filler prose ("À 10 heures, les cérémonies uDP exigent soit…").

    # Correlation guarantee: the cross-source chunks retrieved FOR an event
    # (its L-rows, "(lié à: <event>) …") ride into whichever thread carries
    # that event. The plan routinely drops them — and a writer can only state
    # the link it was shown; without the WhatsApp facts beside the calendar
    # row it free-associates on the title instead ("chaussures GR200" for a
    # door closer).
    used_ids = {i for t in threads for i in t["ids"]}
    corr_by_event: dict[str, list[str]] = {}
    for e in registry:
        if e["kind"] != "L" or e["id"] in used_ids:
            continue
        m = re.match(r"^\(lié à:\s*(.+?)\)", e["text"])
        if m:
            key = _norm_key(m.group(1))[:_TITLE_MATCH_PREFIX]
            corr_by_event.setdefault(key, []).append(e["id"])
    for thread in threads:
        if not corr_by_event:
            break
        for i in list(thread["ids"]):
            anchor = by_id.get(i)
            if anchor is None or anchor["kind"] != "A":
                continue
            key = _norm_key(_event_title(anchor["text"]))[:_TITLE_MATCH_PREFIX]
            for lid in corr_by_event.pop(key, []):
                if lid not in used_ids and len(thread["ids"]) < 6:
                    thread["ids"].append(lid)
                    used_ids.add(lid)
                    log.info("composer: correlated row %s attached to its anchor's thread", lid)

    # READINESS + lede run BEFORE the thread writers, not for data reasons
    # (they only need the plan + rows) but for swap economy: in two-quills
    # mode they share the plan's tier (Gemma), while the writers, the lede
    # challenger and the judges share the other (Ministral) — this order
    # makes the composition exactly two model residencies instead of four.
    readiness = await _write_readiness(llm, rows, language, advice=advice)
    lede = await _write_lede(
        llm,
        threads,
        by_id,
        registry,
        date_str,
        day_anchor,
        language=language,
        foreign_nouns=foreign_nouns,
        n_candidates=max(1, bestof_n),
        budget=budget,
    )

    paragraphs: list[str] = []
    for thread in threads:
        # News rows are periphery candidates only — woven into MY DAY they
        # would be exactly the personal/world fusion the composer exists to
        # prevent (the plan's prompt says so, but a 14B needs the code to).
        entries = [by_id[i] for i in thread["ids"] if i in by_id and by_id[i]["kind"] != "N"]
        if not entries:
            continue
        # The angle is free text written by the plan model — which saw the
        # news rows. It is the one channel through which world content reaches
        # a thread writer, so a news-bearing angle is neutralised in code.
        own_blob = " ".join(e["text"] for e in entries)
        angle_leaks = _noun_leaks(thread.get("angle") or "", foreign_nouns, own_blob)
        if angle_leaks:
            log.info(
                "composer: angle carried news content (%s) — neutralised",
                ", ".join(angle_leaks)[:120],
            )
            thread = {**thread, "angle": _NEUTRAL_ANGLE}
        # Real back-to-back chains touching this thread ride along as facts —
        # the adjacency IS the consequence to state ("the review ends 17:00
        # sharp and the sync starts on it"), and a fact the writer was shown
        # must also count as allowed in the verification.
        adjacencies = _thread_adjacencies(chained or [], entries)
        verify_entries = entries + [
            {"when": a["at"], "text": f"{a['from']} {a['to']}", "kind": "adj"} for a in adjacencies
        ]
        verify_blob = " ".join(f"{e.get('when') or ''} {e['text']}" for e in verify_entries)

        def _all_violations(p: str) -> list[str]:
            v = paragraph_violations(p, verify_entries, date_str)
            v += [
                f"« {t} » vient des actualités, pas des faits du fil — aucune "
                "référence à l'actualité dans ce paragraphe"
                for t in _noun_leaks(p, foreign_nouns, verify_blob)
            ]
            return v

        para = await _write_paragraph(
            llm,
            thread,
            entries,
            date_str,
            day_anchor,
            critic_feedback,
            adjacencies=adjacencies,
            language=language,
        )
        violations = _all_violations(para)
        if violations:
            log.info("composer: paragraph regenerated (%s)", "; ".join(violations)[:160])
            para = await _write_paragraph(
                llm,
                thread,
                entries,
                date_str,
                day_anchor,
                critic_feedback,
                violations,
                adjacencies=adjacencies,
                language=language,
            )
            if _all_violations(para):
                # Still off after one retry — keep the draft; the downstream
                # lint/critic loop sees the assembled text and can repair.
                log.warning("composer: paragraph still violates after retry")
        elif bestof_n >= 2 and (budget is None or not budget.exceeded()):
            # The first candidate is clean — buy a second and let a binary
            # judge pick. A 14B compares two texts far more reliably than it
            # writes one perfect text; this is where the 1h budget goes.
            alt = (
                await _write_paragraph(
                    llm,
                    thread,
                    entries,
                    date_str,
                    day_anchor,
                    critic_feedback,
                    adjacencies=adjacencies,
                    language=language,
                )
            ).strip()
            if alt and alt != para and not _all_violations(alt):
                para = await judge_pick(
                    llm,
                    [para, alt],
                    facts=verify_blob[:2400],
                    criteria=(
                        "Direct chief-of-staff prose: states the practical "
                        "consequence and the real link between the facts; no "
                        "filler, no coach advice, no corporate jargon."
                    ),
                    language=language,
                )
        paragraphs.append(f"{para.strip()} {_src_marker(entries)}".strip())

    if not paragraphs:
        raise ComposerError("no paragraph survived composition")

    # Cohesion pass: a single rewrite that may reorder/link the paragraphs but
    # provably adds no facts (markers preserved, figures/hours/dates ⊆ input);
    # rejected output keeps the originals.
    paragraphs = await _cohere_paragraphs(llm, paragraphs, day_anchor, language)

    around_items = _decay_seen_around(
        filter_around(plan.get("around") or [], by_id, date_str, language), date_str
    )
    entry_titles = _other_entry_titles(registry)
    around_lines: list[str] = []
    for item in around_items:
        e = item["entry"]
        stake = (item.get("stake") or "").strip().rstrip(".")
        # "…, sans lien avec les priorités du jour" — the plan's favourite
        # filler clause, never load-bearing. The comma is required so real
        # content ("à rendre sans faute") survives the cut.
        stake = re.sub(r",\s+sans\s+[^,.;]{3,80}$", "", stake).strip()
        if stake:
            contaminated = _stake_contaminations(stake, e, entry_titles)
            leaked = _noun_leaks(stake, foreign_nouns, e["text"])
            unsupported = _stake_unsupported_words(stake, e)
            if contaminated or leaked or unsupported:
                log.info(
                    "composer: stake rejected (%s) — code-rendered fact used",
                    "; ".join(contaminated + leaked + unsupported)[:120],
                )
                stake = ""
        if not stake:
            stake = _fact_fallback(e, language)
        if not stake:
            continue
        when = _human_date(e["date"]) if e.get("date") else _human_date(e["when"])
        around_lines.append(f"- {stake} [src: {e['label']}{' · ' + when if when else ''}]")

    # Persist what this briefing put in orbit so tomorrow's can open with a
    # callback ("↩ Hier, le briefing préparait X — c'est aujourd'hui à 15:00").
    try:
        prepared = [
            ((item["entry"].get("title") or "").strip() or _event_title(item["entry"]["text"]))[:80]
            for item in around_items
        ]
        save_state(Path(resolve_data_dir()), build_state(date_str, prepared, lede))
    except Exception:  # noqa: BLE001 — continuity is best-effort
        log.warning("composer: continuity state not persisted")

    parts: list[str] = []
    if readiness:
        parts.append(readiness)
    if lede:
        # An empty OBJECTIVE: line would make the renderer's splitter promote
        # the first prose paragraph to subtitle — omit the line instead.
        parts.append(f"OBJECTIVE: {lede}")
    # Continuity callbacks are code-built facts (yesterday's orbit landing on
    # today's calendar) — they open the narrative before the thread prose.
    parts.append("\n\n".join([*(callbacks or []), *paragraphs]))
    around_intro = (
        "AROUND: Ce qui orbite autour de la journée sans rien exiger aujourd'hui."
        if _is_french(language)
        else "AROUND: What orbits the day without demanding anything from it today."
    )
    parts.append(around_intro + ("\n" + "\n".join(around_lines) if around_lines else ""))
    return "\n\n".join(parts)


class ComposerError(RuntimeError):
    """Composition failed in a way the single-pass path should absorb."""


def _thread_adjacencies(chained: list[dict], entries: list[dict]) -> list[dict]:
    """The chained pairs whose endpoints appear in this thread's rows.

    Full normalised-title containment — a prefix match would leak a chain
    into any thread whose rows merely share the first words of a title.
    """
    blob = _norm_key(" ".join(e["text"] for e in entries))
    out = []
    for c in chained:
        endpoints = (_norm_key(str(c.get("from") or "")), _norm_key(str(c.get("to") or "")))
        if any(e and e in blob for e in endpoints):
            out.append(c)
    return out


async def _write_paragraph(
    llm: ComposerLlm,
    thread: dict,
    entries: list[dict],
    date_str: str,
    day_anchor: str,
    critic_feedback: str,
    violations: list[str] | None = None,
    adjacencies: list[dict] | None = None,
    language: str = "French",
) -> str:
    prompt = render_prompt(
        "briefing_thread_writer",
        date_str=date_str,
        day_anchor=day_anchor,
        angle=thread.get("angle") or "",
        entries=entries,
        critic_feedback=critic_feedback,
        violations=violations or [],
        adjacencies=adjacencies or [],
        language=language,
    )
    # Cloud-quality style anchors (data-dir bank, possibly absent). The hour/
    # date containment already guards against an exemplar's facts leaking in.
    examples = exemplar_block("writer", language)
    if examples:
        prompt = f"{prompt}\n\n{examples}"
    return (await llm(prompt, **_WRITER_OPTS)).strip()


def _day_shape(registry: list[dict], language: str) -> tuple[list[str], str, str]:
    """Code-derived shape of the day from the A-rows: (stat facts, first
    timed event's title, its clock time).

    These are the lede's safety net — both the prompt's "REPÈRES" block and
    the deterministic fallback sentence are built from them, so the opening
    line can always name something real."""
    a_entries = [e for e in registry if e["kind"] == "A"]
    hours: list[str] = []
    first_title, first_hour = "", ""
    for e in a_entries:
        found = sorted(_hours_in(e.get("when") or ""))
        if found and not first_title:
            # The full event title — real titles carry colons ("Daily : Data
            # Lake"), so no "title:" splitting here (A-row text is just the
            # bracketed group + title).
            first_title = _event_title(e["text"])[:80]
            first_hour = found[0]
        hours.extend(found)
    hours.sort(key=lambda h: (int(h.split(":")[0]), int(h.split(":")[1])))
    fr = _is_french(language)
    stats: list[str] = []
    if a_entries:
        stats.append(
            f"{len(a_entries)} événement(s) aujourd'hui"
            if fr
            else f"{len(a_entries)} event(s) today"
        )
    if hours:
        first, last = hours[0], hours[-1]
        stats.append(
            f"premier à {first}, dernier à {last}" if fr else f"first at {first}, last at {last}"
        )
    return stats, first_title, first_hour


def _fallback_lede(registry: list[dict], language: str) -> str:
    """Deterministic, always-presentable opening line from the day's own rows.

    Ships when every model candidate failed the concreteness lint — plain but
    true beats polished and empty."""
    stats, first_title, first_hour = _day_shape(registry, language)
    n = sum(1 for e in registry if e["kind"] == "A")
    if not first_title:
        return ""
    at = f" à {first_hour}" if _is_french(language) and first_hour else ""
    if not _is_french(language):
        at = f" at {first_hour}" if first_hour else ""
        rest = f" — {n} events on the day" if n > 1 else ""
        return f"The day opens on {first_title}{at}{rest}."
    rest = f" — {n} rendez-vous au programme" if n > 1 else ""
    return f"La journée s'ouvre sur {first_title}{at}{rest}."


async def _write_lede(
    llm: ComposerLlm,
    threads: list[dict],
    by_id: dict,
    registry: list[dict],
    date_str: str,
    day_anchor: str,
    *,
    language: str = "French",
    foreign_nouns: set[str] | None = None,
    n_candidates: int = 2,
    budget: TimeBudget | None = None,
) -> str:
    """The briefing's opening line — best-of-N, lint-prefiltered, A/B-judged.

    The candidate pool is written from the PIVOT thread's facts plus the
    code-derived day shape; ``lede_issues`` kills mission statements before
    they reach the judge, and the hour/date containment check kills invented
    anchors. When the whole pool fails, the code-built fallback ships.
    """
    pivot_ids = threads[0]["ids"] if threads else []
    pivot = [by_id[i] for i in pivot_ids if i in by_id and by_id[i]["kind"] != "N"]
    stats, _, _ = _day_shape(registry, language)
    # The lede may name any of the day's own anchors, not just the pivot's —
    # verification reads the full personal registry.
    verify_entries = [e for e in registry if e["kind"] != "N"]

    def _bad(cand: str) -> list[str]:
        issues = lede_issues(cand)
        issues += paragraph_violations(cand, verify_entries, date_str)
        issues += _noun_leaks(cand, foreign_nouns or set(), "")
        return issues

    candidates: list[str] = []
    prompt = render_prompt(
        "briefing_lede",
        date_str=date_str,
        day_anchor=day_anchor,
        entries=pivot,
        stats=stats,
        language=language,
    )
    examples = exemplar_block("lede", language)
    if examples:
        prompt = f"{prompt}\n\n{examples}"
    # The pool spans both quills when routing is on: ``n_candidates`` from the
    # "lede" tier plus ONE challenger from the "lede_alt" tier (unrouted, the
    # stages resolve to the same model and the challenger is just one more
    # candidate). The bench showed the two families fail differently here —
    # Gemma sober but flat, Ministral vivid but drifting into metaphor — so
    # the tournament gets to pick across the trade-off instead of within it.
    plans = [_LEDE_OPTS] * max(1, n_candidates) + [{**_LEDE_OPTS, "stage": "lede_alt"}]
    for i, opts in enumerate(plans):
        if i and budget is not None and budget.exceeded():
            break
        try:
            # A 14B loves wrapping its one important line in **bold** — strip
            # edge emphasis so the subtitle isn't an all-bold paragraph.
            cand = (await llm(prompt, **opts)).strip().strip("*").strip()
        except Exception as exc:  # noqa: BLE001 — the fallback always ships
            log.warning("composer: lede candidate failed (%r)", exc)
            continue
        issues = _bad(cand)
        if issues:
            log.info("composer: lede candidate rejected (%s)", "; ".join(map(str, issues))[:120])
            continue
        candidates.append(cand)
    if not candidates:
        lede = _fallback_lede(registry, language)
        log.info("composer: lede fell back to the code-built line")
        return lede
    if len(candidates) == 1:
        return candidates[0]
    facts_blob = " ".join(f"{e.get('when') or ''} {e['text']}" for e in pivot)
    return await judge_pick(
        llm,
        candidates,
        facts=f"{facts_blob[:2000]}\n{'; '.join(stats)}",
        criteria=(
            "The single opening line of the briefing: concrete beats abstract "
            "— real events, real times, the arc of the day; no jargon, no "
            "mission statement, no advice."
        ),
        language=language,
    )


# Cohesion rewrite: 2-5 single-line paragraphs separated by blank lines.
_COHESION_GBNF = r"""
root ::= para ("\n\n" para){1,4}
para ::= [^\n] [^\n]+
"""
_COHESION_OPTS = {
    "max_tokens": 700,
    "temperature": 0.3,
    "gbnf_grammar": _COHESION_GBNF,
    "timeout": 360.0,
    "stage": "cohesion",
}
_SRC_MARKER_RE = re.compile(r"\[src:[^\]]+\]")


async def _cohere_paragraphs(
    llm: ComposerLlm, paragraphs: list[str], day_anchor: str, language: str = "French"
) -> list[str]:
    """One rewrite to turn isolated thread paragraphs into flowing prose.

    The writers are deliberately blind to each other; this pass sees them all
    — but is allowed to ADD nothing: the rewrite ships only if every [src: …]
    marker survives verbatim and its hours/dates/figures are a subset of the
    input's. Anything else keeps the original paragraphs (cost: one ~60s call).
    """
    if len(paragraphs) < 2:
        return paragraphs
    body = "\n\n".join(paragraphs)
    try:
        out = (
            await llm(
                render_prompt(
                    "briefing_cohesion", body=body, day_anchor=day_anchor, language=language
                ),
                **_COHESION_OPTS,
            )
        ).strip()
    except Exception as exc:  # noqa: BLE001 — optional pass, never blocks
        log.warning("composer: cohesion pass failed (%r) — keeping originals", exc)
        return paragraphs
    if sorted(_SRC_MARKER_RE.findall(out)) != sorted(_SRC_MARKER_RE.findall(body)):
        log.info("composer: cohesion rejected (markers altered)")
        return paragraphs
    if _hours_in(out) - _hours_in(body):
        log.info("composer: cohesion rejected (new hours)")
        return paragraphs
    in_dates = set()
    for _, c in extract_date_mentions(body):
        in_dates |= c
    for _, c in extract_date_mentions(out):
        if not (c & in_dates):
            log.info("composer: cohesion rejected (new date)")
            return paragraphs
    in_figures = {d for d, _ in extract_unit_numbers(body)}
    if {d for d, _ in extract_unit_numbers(out)} - in_figures:
        log.info("composer: cohesion rejected (new figure)")
        return paragraphs
    log.info("composer: cohesion pass accepted")
    return [p for p in out.split("\n\n") if p.strip()]


_NUMBER_TOKEN_RE = re.compile(r"\d+(?:[.,]\d+)?")

# A READINESS line asserting a SCHEDULED workout — sport vocabulary within
# reach of a commitment word ("ta séance de musculation prévue", "ton
# entraînement programmé"). Legitimate only when the calendar itself carries
# the sport (``advice["planned"]``); a workout that lives only in the user's
# notes is a programme, not an appointment, and claiming otherwise is the
# exact hallucination Gemma shipped in the 2026-06-12 bench.
_SPORT_WORDS = r"(?:séance|seance|musculation|entraînement|entrainement|sport|course|footing|running|workout|sortie)"
_CLAIM_WORDS = r"(?:prévu(?:e|s|es)?|planifié(?:e|s|es)?|programmé(?:e|s|es)?|au\s+programme|inscrit(?:e)?|scheduled|planned)"
_SPORT_SCHEDULE_CLAIM_RE = re.compile(
    rf"{_SPORT_WORDS}\W+(?:\w+\W+){{0,6}}{_CLAIM_WORDS}|{_CLAIM_WORDS}\W+(?:\w+\W+){{0,6}}{_SPORT_WORDS}",
    re.IGNORECASE,
)


def _schedule_claim(line: str, advice: dict | None) -> str:
    """The scheduled-sport assertion ``line`` makes without calendar backing.

    Returns the offending excerpt (empty = clean). When the calendar itself
    carries a sport activity (``advice["planned"]``) any phrasing is fine.
    """
    if (advice or {}).get("planned"):
        return ""
    m = _SPORT_SCHEDULE_CLAIM_RE.search(line or "")
    return m.group(0) if m else ""


# WHOOP day strain is a 0–21 scale. A READINESS line that calls the strain
# "élevé"/"gros"/"intense" — or advises easing off "après un effort" — while the
# most recent night actually logged a low strain (a rest day) is the exact
# contresens shipped on 2026-06-22 (strain 0.3 read as "strain élevé", advising
# light walks). The figure check pins the numbers; this pins the adjective.
_STRAIN_VALUE_RE = re.compile(r"strain\s+(\d+(?:[.,]\d+)?)", re.IGNORECASE)
_HIGH_STRAIN_CLAIM_RE = re.compile(
    r"strain\s+(?:[ée]lev[ée]e?|fort|intense|important)|gros\s+strain|"
    r"effort(?:s)?\s+(?:soutenu|intense|important)|apr[èe]s\s+(?:un|l['’])?\s*(?:gros\s+)?effort",
    re.IGNORECASE,
)
_LOW_STRAIN_MAX = 8.0  # below this the day was light/rest; "élevé" would be a lie


def _strain_conflict(line: str, latest_row: str) -> str:
    """The strain adjective ``line`` asserts that the latest night contradicts.

    Returns the offending strain value (empty = clean). Only the clear
    contradiction — a low-strain night described as high-strain — is flagged;
    the inverse is left to the figure check, which already pins the number.
    """
    m = _STRAIN_VALUE_RE.search(latest_row or "")
    if not m:
        return ""
    strain = float(m.group(1).replace(",", "."))
    if strain < _LOW_STRAIN_MAX and _HIGH_STRAIN_CLAIM_RE.search(line or ""):
        return f"strain {m.group(1)}"
    return ""


async def _write_readiness(
    llm: ComposerLlm, rows: dict, language: str = "French", advice: dict | None = None
) -> str:
    """One-line READINESS steer from the health rows (empty when no health).

    ``advice`` is the day-load adviser's code-derived recommendation
    (:func:`estormi_briefing.day.day_load.choose_advice`): its facts — the free
    slot, the workout note mined from the user's own notes, the meeting span —
    ride into the prompt so the steer can be CONCRETE ("ta séance du carnet
    passe sur le créneau de midi") instead of generic, and their figures join
    the allowed set so the verification doesn't kill the slot times.

    Any figure the line cites must literally exist in the health rows or the
    advice facts — a one-digit slip (66% read back as 61%) erodes trust faster
    than no figure at all. A violating figure gets one rewrite; if the rewrite
    still cites a phantom figure, the line is omitted entirely.
    """
    health = rows.get("health_rows") or []
    if not health:
        return ""
    advice_facts = list((advice or {}).get("facts") or [])
    # Figures may be cited ONLY from the MOST RECENT night (health is sorted
    # newest-first by _fetch_health_chunks). Older rows still ride into the
    # prompt as trend context, but their numbers are NOT citable — else
    # yesterday's recovery (98%) reads back as today's when today's was 75%.
    latest = str(health[0])
    allowed = {n.replace(",", ".") for n in _NUMBER_TOKEN_RE.findall(latest)}
    allowed |= {n.replace(",", ".") for f in advice_facts for n in _NUMBER_TOKEN_RE.findall(f)}

    def _phantoms(line: str) -> list[str]:
        return [n for n in _NUMBER_TOKEN_RE.findall(line) if n.replace(",", ".") not in allowed]

    prompt = render_prompt(
        "briefing_readiness",
        health_rows=health,
        advice_facts=advice_facts,
        advice_kind=(advice or {}).get("kind") or "",
        language=language,
    )
    examples = exemplar_block("readiness", language)
    if examples:
        prompt = f"{prompt}\n\n{examples}"
    try:
        out = (await llm(prompt, **_READINESS_OPTS)).strip()
        bad = _phantoms(out)
        claim = _schedule_claim(out, advice)
        strain_bad = _strain_conflict(out, latest)
        if bad or claim or strain_bad:
            corrections = []
            if bad:
                corrections.append(
                    f"les chiffres {', '.join(bad)} n'existent pas dans les données — cite "
                    "uniquement un chiffre présent tel quel, ou aucun"
                )
            if claim:
                corrections.append(
                    f"« {claim} » affirme une séance PLANIFIÉE alors que l'agenda n'en porte "
                    "aucune — la séance vient des notes : propose-la (« le créneau est idéal "
                    "pour »), ne la déclare jamais prévue"
                )
            if strain_bad:
                corrections.append(
                    f"la nuit la plus récente affiche un {strain_bad} FAIBLE (journée de "
                    "repos) — ne décris pas un strain élevé ni un effort dont il faudrait "
                    "récupérer"
                )
            log.info("composer: readiness rewrite (%s)", "; ".join(corrections)[:160])
            out = (
                await llm(
                    prompt + "\nATTENTION : " + " ; ".join(corrections) + ".",
                    **_READINESS_OPTS,
                )
            ).strip()
            if _phantoms(out) or _schedule_claim(out, advice) or _strain_conflict(out, latest):
                # No READINESS beats a wrong one; the briefing renders fine
                # without the line.
                log.warning("composer: readiness still violates after rewrite — omitted")
                return ""
    except Exception as exc:  # noqa: BLE001 — optional line, never blocks
        log.warning("composer: readiness failed (%r) — omitted", exc)
        return ""
    return out
