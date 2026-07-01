"""Prompt construction and LLM-output parsing for the briefing engine.

Pure, side-effect-free helpers: every function here either builds a prompt
string (from a Jinja template via ``render_prompt``) or parses an LLM reply
back into bullets. They read the run's output language and the user's display
names from ``runtime`` but make no network or DB calls — the orchestration
that actually invokes the LLM lives in ``run_briefing`` and its siblings.
"""

from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable

import structlog

from estormi_briefing.compose import graph
from estormi_briefing.llm import runtime
from memory_core.labels import is_opaque_label as _is_opaque_label
from memory_core.prompt_templates import render as render_prompt
from memory_core.sanitizer import sanitize_chunk, strip_calendar_sync_footer

log = structlog.get_logger()

# ── Per-source prompt builders ─────────────────────────────────────────────────


def _common_prompt_rules(source_label: str, date_str: str) -> str:
    """Return the shared rules block injected into every per-source prompt."""
    return render_prompt(
        "knowledge_common_rules",
        source_label=source_label,
        date_str=date_str,
        language=runtime.language,
    ).strip()


def narration_prompt(body: str, title: str) -> str:
    """Build the prompt that re-voices the composed briefing for listening.

    The on-screen briefing body reads awkwardly aloud (percentages, clock
    forms, deltas, section headers, citations). This asks the LLM for a spoken
    edition — the same facts, built for the ear — which is then fed to the TTS
    model. ``title`` anchors the opening line so the model never invents a
    greeting or guesses the weekday. See
    ``delivery._generate_spoken_briefing``.
    """
    return render_prompt(
        "knowledge_narration",
        body=body,
        title=title,
        language=runtime.language,
    )


def _news_prompt(source_label: str, date_str: str, text: str) -> str:
    """Build the news-mode summarisation prompt for a video transcript."""
    return render_prompt(
        "knowledge_news",
        source_label=source_label,
        text=text,
        common_rules=_common_prompt_rules(source_label, date_str),
    )


def _analysis_prompt(source_label: str, date_str: str, text: str) -> str:
    """Build the analysis-mode prompt for a video transcript."""
    return render_prompt(
        "knowledge_analysis",
        source_label=source_label,
        text=text,
        common_rules=_common_prompt_rules(source_label, date_str),
    )


def _opinion_prompt(source_label: str, date_str: str, text: str) -> str:
    """Build the opinion-mode prompt."""
    return render_prompt(
        "knowledge_opinion",
        source_label=source_label,
        text=text,
        common_rules=_common_prompt_rules(source_label, date_str),
    )


# Framing line for sources flagged ``promotional: true`` — their content is
# the vendor talking about its own product, and the briefing must keep that
# distance instead of restating marketing copy first-degree.
_PROMOTIONAL_NOTE = (
    "SOURCE FRAMING — this source promotes its own product/company: treat its "
    "claims as commercial discourse, keep critical distance, and make the "
    "self-interest visible in the phrasing (e.g. 'the company claims…'). "
    "Never restate superlatives or sales claims as established facts.\n"
)


def _make_prompt(
    mode: str,
    source_label: str,
    date_str: str,
    text: str,
    pre_prompt: str = "",
    promotional: bool = False,
) -> str:
    """Build a video-transcript prompt for the source's mode.

    An optional per-source ``pre_prompt`` is wrapped in a tagged priority
    block — the YouTube-side equivalent of the RSS ``pre_prompt`` field.
    The tagged block frames the guidance as the highest-priority instruction
    so the LLM respects it even when the base prompt's rules push the other
    way (the unwrapped prepend was sometimes ignored).
    """
    if mode == "opinion":
        base = _opinion_prompt(source_label, date_str, text)
    elif mode == "analysis":
        base = _analysis_prompt(source_label, date_str, text)
    else:
        base = _news_prompt(source_label, date_str, text)
    blocks: list[str] = []
    if promotional:
        blocks.append(_PROMOTIONAL_NOTE)
    pre = (pre_prompt or "").strip()
    if pre:
        blocks.append(
            "PRIORITY USER GUIDANCE (to respect in both the selection and the "
            "phrasing of the items; takes precedence over the general rules in "
            f"case of conflict):\n{pre}\n"
        )
    if not blocks:
        return base
    return "\n".join([*blocks, base])


def _make_rss_prompt(source: dict, articles_text: str, date_str: str) -> str:
    """Build the RSS synthesis prompt for a single source."""
    pre_prompt = source.get("pre_prompt", "")
    label = source["label"]
    instruction = (
        pre_prompt
        if pre_prompt
        else (
            f"You are a press synthesiser. Source: {label}. "
            f"Synthesise the notable articles from the last 24h."
        )
    )
    # The framing rides OUTSIDE the template's untrusted <user_instruction>
    # block: inside it, the template itself tells the model to treat the text
    # as data, which would neuter the instruction.
    return render_prompt(
        "knowledge_rss",
        instruction=instruction,
        framing=_PROMOTIONAL_NOTE if source.get("promotional") else "",
        articles_text=articles_text,
        common_rules=_common_prompt_rules(label, date_str),
    )


def _format_rss_articles(articles: list[dict]) -> str:
    parts = []
    for a in articles:
        title = a.get("title", "")
        summary = a.get("summary", "")
        published = a.get("published", "")
        line = f"[{published}] {title}"
        if summary:
            line += f"\n  {summary}"
        parts.append(line)
    return "\n\n".join(parts)


def _extract_topics_from_items(news_items: list[dict]) -> list[str]:
    """Extract a compact list of topic snippets from raw news items.

    Used as a fallback to persist continuity data even when the synthesis
    LLM call returns empty output.  Returns up to 15 entries of the form
    "[Source] first-8-words-of-bullet".
    """
    topics: list[str] = []
    for item in news_items:
        label = item.get("source_label") or "Source"
        for raw in item.get("bullets", []):
            b = str(raw).strip().lstrip("- ").lstrip("• ").lstrip("[").split("]")[-1].strip()
            words = b.split()[:8]
            if words:
                topics.append(f"[{label}] {' '.join(words)}")
            if len(topics) >= 15:
                return topics
    return topics


def _personal_context_block(calendar: list[dict], last_briefing_topics: str) -> str:
    """Build a personal-context preamble for synthesis prompts."""
    lines: list[str] = []
    if calendar:
        # Separate multi-day (all-day spanning events) from timed events
        multiday = [ev for ev in calendar if ev.get("when") == "All day"]
        timed = [ev for ev in calendar if ev.get("when") != "All day"]

        if multiday:
            lines.append("ONGOING EVENTS (multi-day or all-day):")
            for ev in multiday:
                # Calendar titles are untrusted (external invitees) — neutralise
                # injection markers before they reach the synthesis prompt.
                title = sanitize_chunk(ev.get("title") or "") or "(untitled)"
                lines.append(f"  - {title}")
            lines.append("")

        if timed:
            lines.append("TODAY'S SCHEDULE (the user's meetings):")
            for ev in timed:
                when = ev.get("when") or "All day"
                title = sanitize_chunk(ev.get("title") or "") or "(untitled)"
                lines.append(f"  - {when}: {title}")
            lines.append("")
    if last_briefing_topics:
        lines.append("LAST BRIEFING TOPICS (for continuity):")
        lines.append(f"  {last_briefing_topics}")
        lines.append("")
    return "\n".join(lines)


def _safe_bullet(raw: object) -> str:
    """Return a single bullet ready to embed inside a delimited prompt block.

    Runs ``sanitize_chunk`` to neutralise the usual injection markers and
    drops any closing-delimiter substrings (``</sources>``, ``</items>``,
    ``</personal_context>``) so a hostile feed summary can't break out of
    the surrounding ``<sources>…</sources>`` / ``<items>…</items>`` block.
    """
    text = str(raw).strip().lstrip("- ").lstrip("• ")
    if not text:
        return ""
    text = sanitize_chunk(text)
    for closer in ("</sources>", "</items>", "</personal_context>"):
        text = text.replace(closer, closer.replace("<", "<​"))
    return text


# Trailing "(Source, YYYY-MM-DD)" on an input bullet — its real date.
_BULLET_DATE_RE = re.compile(r"\(([^()]*),\s*(\d{4}-\d{2}-\d{2})\)\s*$")
# A trailing citation the model appends: [3] or [1, 4].
_CITE_RE = re.compile(r"\[\s*(\d+(?:\s*,\s*\d+)*)\s*\]\s*$")


def _numbered_news(news_items: list[dict], date_str: str) -> tuple[str, dict[int, dict]]:
    """Number every input news bullet and map index → {source, date}.

    The synthesis model cites these numbers; :func:`resolve_news_citations`
    turns them back into real ``[SOURCE: … | date]`` markers. Attribution thus
    never depends on the model re-emitting names/dates (where weak models drift
    or hallucinate) — the code owns it."""
    parts: list[str] = []
    index: dict[int, dict] = {}
    n = 0
    for item in news_items:
        label = item.get("source_label") or "Unknown source"
        for bullet in item.get("bullets", []):
            b = _safe_bullet(bullet)
            if not b:
                continue
            n += 1
            dm = _BULLET_DATE_RE.search(b)
            index[n] = {"source": label, "date": dm.group(2) if dm else date_str}
            parts.append(f"[{n}] [{label}] {b}")
    return "\n".join(parts), index


_BULLET_KIND_RE = re.compile(r"^[-•]?\s*(?:\[[a-z]+\]\s*)?", re.IGNORECASE)


def fallback_news_from_items(news_items: list[dict], date_str: str, limit: int = 12) -> str:
    """Deterministic world-news list straight from the input bullets.

    Used when the synthesis model ignored the citation format (weak local models
    do) and citation-resolve kept nothing. Every input bullet is real and
    already carries its source, so this guarantees a non-empty, fully-sourced,
    hallucination-free section — at the cost of cross-source merging."""
    out: list[str] = []
    seen: set[str] = set()
    for item in news_items:
        label = item.get("source_label") or "Unknown source"
        for bullet in item.get("bullets", []):
            b = _safe_bullet(bullet)
            if not b:
                continue
            dm = _BULLET_DATE_RE.search(b)
            date = dm.group(2) if dm else date_str
            text = (b[: dm.start()] if dm else b).rstrip()
            text = _BULLET_KIND_RE.sub("", text).strip()
            if not text:
                continue
            key = re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()[:80]
            if key in seen:
                continue
            seen.add(key)
            out.append(f"- {text} [SOURCE: {label} | {date}]")
            if len(out) >= limit:
                return "\n".join(out)
    return "\n".join(out)


def fallback_themes_from_items(other_items: list[dict], date_str: str) -> str:
    """Deterministic theme blocks straight from the input items.

    Used when the synthesis model didn't emit the ``THEME:``/``SOURCE:``
    structure (weak local models drift to markdown). Renders one ``SOURCE:``
    block per source with its real label/date and a summary built from its own
    bullets — clean, sourced, in the canonical format ``_render_themes_html``
    already styles. No cross-theme clustering, but never a mangled section."""
    blocks: list[str] = []
    for item in other_items:
        label = item.get("source_label") or "Source"
        texts: list[str] = []
        date = date_str
        for bullet in item.get("bullets", []):
            b = _safe_bullet(bullet)
            if not b:
                continue
            dm = _BULLET_DATE_RE.search(b)
            if dm:
                date = dm.group(2)
            t = (b[: dm.start()] if dm else b).rstrip()
            t = _BULLET_KIND_RE.sub("", t).strip()
            if t:
                texts.append(t)
        if not texts:
            continue
        blocks.append(f"SOURCE: {label} |  | {date}")
        blocks.append(" ".join(texts)[:600])
        blocks.append("")
    return "\n".join(blocks).strip()


def resolve_news_citations(text: str, sources_index: dict[int, dict]) -> str:
    """Replace each bullet's trailing ``[n]``/``[n,m]`` with the real
    ``[SOURCE: … | date]`` from ``sources_index``. A bullet with no valid
    citation is DROPPED — ungrounded in the inputs, it is almost always a
    fabrication. Non-bullet lines (separators, headings) pass through."""
    out: list[str] = []
    for raw in text.splitlines():
        line = raw.rstrip()
        stripped = line.lstrip()
        if not (stripped.startswith("- ") or stripped.startswith("• ")):
            out.append(raw)
            continue
        m = _CITE_RE.search(line)
        if not m:
            continue  # uncited → drop
        refs = [
            sources_index[k]
            for k in (int(x) for x in re.findall(r"\d+", m.group(1)))
            if k in sources_index
        ]
        if not refs:
            continue  # citation points at nothing real → drop
        sources: list[str] = []
        for r in refs:
            if r["source"] not in sources:
                sources.append(r["source"])
        dates = [r["date"] for r in refs if r.get("date")]
        marker = "[SOURCE: " + " · ".join(sources) + (f" | {max(dates)}" if dates else "") + "]"
        out.append(f"{line[: m.start()].rstrip()} {marker}")
    return "\n".join(out)


def _news_synthesis_prompt(
    news_items: list[dict],
    date_str: str,
    personal_context: str = "",
) -> str:
    """Build the cross-source news synthesis prompt."""
    joined, _ = _numbered_news(news_items, date_str)

    context_section = ""
    if personal_context:
        context_section = f"PERSONAL CONTEXT:\n{personal_context}\n"

    calendar_signal_rule = ""
    if personal_context and "TODAY'S SCHEDULE" in personal_context:
        calendar_signal_rule = (
            "SCHEDULE SIGNAL (📅):\n"
            "If a news topic directly touches a meeting or appointment listed in\n"
            "TODAY'S SCHEDULE above, add the 📅 symbol at the start of the bullet.\n"
            "AT MOST ONE bullet carries it — the single strongest link, or none.\n\n"
        )

    continuity_rule = ""
    if personal_context and "LAST BRIEFING TOPICS" in personal_context:
        continuity_rule = (
            "FOLLOW-UP (↩ Follow-up:):\n"
            "If a topic from today was already in the LAST BRIEFING, add '↩ Follow-up:'\n"
            "at the start of the sentence to indicate continuity.\n\n"
        )

    return render_prompt(
        "knowledge_news_synthesis",
        date_str=date_str,
        user_context=runtime.user_context,
        context_section=context_section,
        calendar_signal_rule=calendar_signal_rule,
        continuity_rule=continuity_rule,
        joined=joined,
        language=runtime.language,
    )


def _themes_prompt(other_items: list[dict], date_str: str) -> str:
    """Build the per-theme clustering prompt for non-news items.

    Per-source ``pre_prompt`` guidance set in the KnowledgeSourcesPanel is
    surfaced as a separate ``source_guidance`` block (label → guidance) so
    the LLM keeps each source's framing instead of regressing to generic
    topic summaries.
    """
    parts = []
    source_guidance: dict[str, str] = {}
    for item in other_items:
        label = item.get("source_label") or "Source"
        for bullet in item.get("bullets", []):
            b = _safe_bullet(bullet)
            if b:
                parts.append(f"[{label}] {b}")
        pre = (item.get("pre_prompt") or "").strip()
        if pre and label not in source_guidance:
            source_guidance[label] = pre
    joined = "\n".join(parts)
    return render_prompt(
        "knowledge_themes",
        date_str=date_str,
        joined=joined,
        language=runtime.language,
        source_guidance=source_guidance,
    )


# ── LLM-output parsing ─────────────────────────────────────────────────────────

_JSON_BLOCK_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)
_ITEM_KINDS = {
    "news",
    "insight",
    "concept",
    "important",
    "prediction",
    "fact",
}
_PREFIX_KINDS = _ITEM_KINDS - {"news"}


def _coerce_date(value: object, fallback: str = "") -> str:
    raw = str(value or fallback).strip()
    if re.fullmatch(r"\d{8}", raw):
        return f"{raw[:4]}-{raw[4:6]}-{raw[6:8]}"
    return raw


def _missing_closers(prefix: str) -> str | None:
    """The bracket closers a truncated-JSON ``prefix`` still needs, or ``None``
    if the prefix is structurally hopeless (e.g. cut inside a string)."""
    stack: list[str] = []
    in_str = False
    esc = False
    for ch in prefix:
        if esc:
            esc = False
        elif in_str:
            if ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
        elif ch == '"':
            in_str = True
        elif ch in "{[":
            stack.append("}" if ch == "{" else "]")
        elif ch in "}]":
            if not stack or stack[-1] != ch:
                return None
            stack.pop()
    return None if in_str else "".join(reversed(stack))


def _repair_truncated_json(text: str) -> object | None:
    """Parse a reply that hit ``max_tokens`` mid-JSON, salvaging what completed.

    A grammar-constrained local decode that runs out of budget stops with
    unclosed brackets — strict parsing then loses EVERY item, where free-form
    text would have degraded gracefully. Walk back to each ``}`` (the end of a
    complete object), append the closers the prefix still needs, and accept
    the first variant that parses.
    """
    cut = text.rfind("}")
    while cut != -1:
        prefix = text[: cut + 1]
        closers = _missing_closers(prefix)
        if closers is not None:
            try:
                return json.loads(prefix + closers)
            except json.JSONDecodeError:
                pass
        cut = text.rfind("}", 0, cut)
    return None


def _extract_json_payload(llm_output: str) -> object | None:
    text = (llm_output or "").strip()
    if not text:
        return None

    fenced = _JSON_BLOCK_RE.search(text)
    if fenced:
        text = fenced.group(1).strip()

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    start = text.find("[")
    end = text.rfind("]")
    if start != -1 and end > start:
        try:
            return json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            pass

    return _repair_truncated_json(text)


def _json_item_to_bullet(item: dict, fallback_source: str = "", fallback_date: str = "") -> str:
    text = " ".join(str(item.get("text") or "").split())
    if not text:
        return ""

    kind = str(item.get("kind") or "insight").strip().lower()
    if kind not in _ITEM_KINDS:
        kind = "insight"

    source = str(item.get("source") or fallback_source).strip()
    date_str = _coerce_date(item.get("date"), fallback_date)
    prefix = f"[{kind}] " if kind in _PREFIX_KINDS else ""
    citation = f" ({source}, {date_str})" if source and date_str else ""
    return f"- {prefix}{text}{citation}"


def _parse_bullets(llm_output: str) -> list[str]:
    payload = _extract_json_payload(llm_output)
    if isinstance(payload, dict):
        for key in ("items", "bullets", "results"):
            if isinstance(payload.get(key), list):
                payload = payload[key]
                break
    if isinstance(payload, list):
        bullets = [_json_item_to_bullet(item) for item in payload if isinstance(item, dict)]
        return [b for b in bullets if b]

    bullets = []
    for line in llm_output.splitlines():
        line = line.strip()
        if line.startswith("- ") or line.startswith("• "):
            bullets.append(line.lstrip("•").strip())
    return bullets


def _consolidation_prompt(
    axis: str,
    mode: str,
    bullets: list[str],
    *,
    pre_prompt: str = "",
    source_label: str = "",
) -> str:
    """Build the within-group consolidation prompt for a single source.

    ``pre_prompt`` (set in the KnowledgeSourcesPanel) is forwarded as
    high-priority editorial guidance — the LLM must follow it when
    deciding which bullets to keep and how to frame them.
    """
    joined = "\n".join(b for b in (_safe_bullet(x) for x in bullets) if b)
    return render_prompt(
        "knowledge_consolidation",
        axis=axis,
        mode=mode,
        joined=joined,
        language=runtime.language,
        pre_prompt=(pre_prompt or "").strip(),
        source_label=source_label,
    )


# ── Day-vision prompt + conversation labelling ─────────────────────────────────

_FRENCH_DAILY_NOTE_TITLE_RE = re.compile(
    r"^\d{1,2}\s+"
    r"(janvier|février|fevrier|mars|avril|mai|juin|juillet|août|aout|septembre|octobre|novembre|décembre|decembre)"
    r"\s+\d{4}$",
    re.IGNORECASE,
)
_SENDER_RE = re.compile(r"^\[([^\]\n]{1,60})\]:", re.MULTILINE)

# Self-sender labels embedded in stored conversation chunks. "Me" is the
# English label; "Moi" is the legacy label the whatsapp-rust task may still
# stage. Filter out both so the user's own name never becomes a conversation
# label.
_SELF_NAMES = frozenset({"Me", "Moi"})


def _is_generated_knowledge_note(chunk: dict) -> bool:
    """Avoid feeding yesterday's generated brief back into today's prompt."""
    if chunk.get("source") != "notes":
        return False
    text = str(chunk.get("text") or "")
    title = str(chunk.get("title") or "")
    # Match both the legacy "Généré par Knowledge Bot" footer and the
    # current "Estormi — Briefing" one so older briefings still get filtered
    # out of the day-context prompt.
    if (
        "Estormi — Briefing" in text
        or "Estormi — Briefing" in title
        or "Généré par Knowledge Bot" in text
        or "Généré par Knowledge Bot" in title
    ):
        return True
    if _FRENCH_DAILY_NOTE_TITLE_RE.match(title) and (
        "Source :" in text
        or "Sources :" in text
        or "Ma journée" in text
        or "Ce qu’il faut savoir" in text
        or "Ce qu'il faut savoir" in text
    ):
        return True
    return False


def _conversation_label(chunk: dict) -> str:
    """Return a human-readable conversation label for day-brief prompts.

    Names are resolved upstream at ingestion time: the WhatsApp connector maps
    phone-number senders/titles to macOS-Contacts names (resolved at login /
    persisted at chat-list retrieval, and resolved at ingestion for still-unknown
    chats) before chunks are stored. So this stays pure — it surfaces the stored
    real name, treats any remaining raw JID / bare number as opaque (dropping it
    → "unknown conversation", which the prompt renders as "a contact"), and never
    surfaces the ``[unknown]`` sender placeholder as a person.
    """
    title = str(chunk.get("title") or "").removeprefix("WhatsApp — ").strip()
    text = str(chunk.get("text") or "")
    senders: list[str] = []
    for sender in _SENDER_RE.findall(text):
        sender = sender.strip()
        if (
            sender
            and sender not in _SELF_NAMES
            # "[unknown]" is the connector's placeholder for an unresolved
            # sender — it is not a person's name and must never be surfaced as
            # "who".
            and sender.lower() != "unknown"
            and sender not in senders
            and not _is_opaque_label(sender)
        ):
            senders.append(sender)

    if title and not _is_opaque_label(title):
        if senders and title in runtime.user_display_names:
            return ", ".join(senders[:3])
        return title
    if senders:
        return ", ".join(senders[:3])
    return "unknown conversation"


def _sanitise_action_titles(rows: list[dict]) -> list[dict]:
    """Shallow-copy calendar/reminder rows with their ``title`` neutralised.

    Calendar event titles in particular can arrive from external invitees
    (Google Calendar), so they are untrusted retrieved content like any chunk
    body. The raw rows are left intact upstream (dedup / topic-term matching
    rely on the original text); only these prompt-bound copies are sanitised.
    """
    out: list[dict] = []
    for row in rows or []:
        copy = dict(row)
        if copy.get("title"):
            copy["title"] = sanitize_chunk(str(copy["title"]))
        out.append(copy)
    return out


def _vision_chunk_row(chunk: dict) -> dict | None:
    """Render one chunk into a sanitised prompt row (shared by the generic
    context block and the per-event correlation clusters). ``None`` if empty."""
    source = chunk.get("source") or "unknown source"
    group_type = chunk.get("group_type") or ""
    title = _conversation_label(chunk) if source == "whatsapp" else chunk.get("title") or "untitled"
    text = sanitize_chunk(
        strip_calendar_sync_footer(chunk.get("text") or "").replace("\n", " ").strip()
    )
    if not text:
        return None
    return {
        "source": source,
        "group_extra": f", group_type={group_type}" if group_type else "",
        "when_label": chunk.get("when_label") or "",
        "date": chunk.get("date_ts") or chunk.get("date") or "",
        "title": sanitize_chunk(title),
        "text": text[:350],
    }


def _extractor_phrases(extracted_facts: dict | None) -> list[str]:
    """Flatten the structured extractor's commitments into salient-term source text.

    The pre-pass already isolated the day's real obligations (open loops,
    high-priority reminders, partner events) as structured JSON — exactly the
    inherently-meaningful material a topic anchor should be drawn from. Pull any
    ``title``/``text`` strings (and bare-string items) out of those lists so
    ``build_topic_terms`` can mine them alongside the calendar titles.
    """
    phrases: list[str] = []
    for key in ("open_loops", "high_priority_reminders", "partner_events", "physical_activities"):
        for item in (extracted_facts or {}).get(key) or []:
            if isinstance(item, str):
                phrases.append(item)
            elif isinstance(item, dict):
                for field in ("title", "text", "what", "name"):
                    val = item.get(field)
                    if isinstance(val, str) and val:
                        phrases.append(val)
    return phrases


def _build_threads(
    date_str: str,
    calendar: list[dict],
    reminders: list[dict],
    wa_chunks: list[dict],
    context_chunks: list[dict],
    extracted_facts: dict | None = None,
) -> list[dict]:
    """Compute the day's code-validated correlation threads for the prompt.

    The deterministic *reduce* step (see ``graph``): build the curated
    anchor vocabularies — a person lexicon (real WhatsApp contacts + the partner)
    and a salient topic-term set (calendar/reminder titles + the extractor's
    commitments) — reshape the already-fetched data into facts, cluster them by
    shared anchor within a short date window, and return only the cross-source
    threads: pre-formed clusters the rewriter physically cannot fuse across.
    Failure is non-fatal: a bad shape just yields no threads and the prompt
    falls back to the loose candidate links.
    """
    try:
        day = date_str[:10]
        wa_items = [
            {
                "label": _conversation_label(c),
                "text": (c.get("text") or "").strip(),
                "date": c.get("date_ts") or "",
            }
            for c in wa_chunks
        ]
        lexicon = graph.build_lexicon(
            [w["label"] for w in wa_items],
            partner_name=runtime.partner_name,
            exclude=runtime.user_display_names,
        )
        # Topic terms come from real commitments only (titles + extractor), never
        # arbitrary note prose — and exclude known person names so a contact's
        # name isn't double-counted as a subject.
        titles = [a.get("title") or "" for a in (calendar or [])] + [
            r.get("title") or "" for r in (reminders or [])
        ]
        topic_terms = graph.build_topic_terms(
            titles,
            extra_texts=_extractor_phrases(extracted_facts),
            exclude=lexicon | runtime.user_display_names,
        )
        context_rows = [
            {
                "source": c.get("source") or "context",
                "title": c.get("title") or "",
                "text": strip_calendar_sync_footer(c.get("text") or ""),
                "when_label": c.get("when_label") or "",
                "date": c.get("date_ts") or "",
            }
            for c in (context_chunks or [])
            if (c.get("source") or "") != "whoop"
        ]
        facts = graph.collect_facts(
            day=day,
            calendar=calendar,
            reminders=reminders,
            wa_items=wa_items,
            context_rows=context_rows,
            lexicon=lexicon,
            topic_terms=topic_terms,
        )
        threads = graph.render_threads(graph.build_threads(facts))
        # THREADS rows carry raw WhatsApp/mail/note bodies verbatim — the richest
        # injection surface in the vision prompt, and the one block that the graph
        # layer (kept pure) does not sanitise. Neutralise them here like every
        # other untrusted block (CONTEXT/WHATSAPP/LINKS) before they reach the LLM.
        for thread in threads:
            for row in thread.get("rows") or []:
                row["title"] = sanitize_chunk(row.get("title") or "")
                row["text"] = sanitize_chunk(row.get("text") or "")
    except Exception as exc:  # noqa: BLE001 — non-blocking by contract
        log.warning("_build_threads failed, no threads surfaced: %s", exc)
        return []
    if threads:
        log.info(
            "correlation graph: %d cross-source thread(s); dominant anchor=%r",
            len(threads),
            threads[0].get("anchor"),
        )
    return threads


def _whatsapp_blocks(wa_chunks: list[dict]) -> list[dict]:
    """Group recent WhatsApp chunks into per-conversation prompt blocks.

    Chunks arrive newest-first (day_context sorts the recent tail date_ts
    DESC). Each conversation block keeps its 4 most recent chunks but renders
    them in CHRONOLOGICAL order, so the block reads top-to-bottom and its LAST
    line is the latest message — otherwise the model meets the oldest line last
    and misreads an already-answered thread as "no reply".
    """
    by_jid: dict[str, list[str]] = {}
    for chunk in wa_chunks or []:
        jid = _conversation_label(chunk)
        group_type = chunk.get("group_type") or "unknown"
        text = sanitize_chunk((chunk.get("text") or "").strip())
        if text:
            by_jid.setdefault(f"{jid} [{group_type}]", []).append(text)
    blocks: list[dict] = []
    for jid, texts in by_jid.items():
        recent_chrono = list(reversed(texts[:4]))
        blocks.append({"label": jid, "texts": [t[:500] for t in recent_chrono]})
    return blocks


def _assemble_vision_rows(
    date_str: str,
    calendar: list[dict],
    reminders: list[dict],
    wa_chunks: list[dict],
    context_chunks: list[dict] | None = None,
    health_chunks: list[dict] | None = None,
    event_correlations: list[dict] | None = None,
    extracted_facts: dict | None = None,
    local_mode: bool = False,
) -> dict:
    """Shape the fetched chunks into the sanitised, capped prompt rows.

    Shared by the vision prompt and the fact-critic: both must see the SAME
    formatted data, or the critic would flag phrasing the writer never saw.
    """
    overdue = [r for r in reminders if r.get("overdue")]
    today_rem = [r for r in reminders if not r.get("overdue")]

    threads = _build_threads(
        date_str, calendar, reminders, wa_chunks, context_chunks or [], extracted_facts
    )

    wa_blocks = _whatsapp_blocks(wa_chunks)

    # The local model's window (n_ctx≈13k) must hold prompt AND reply; at the
    # cloud-sized data caps the rendered prompt runs ~11.5k tokens and the
    # reply budget collapses below what the vision needs (observed truncation
    # mid-AROUND). Trim the two bulkiest, least-dense blocks for local — the
    # generic context tail and the news digest — and keep the high-signal
    # blocks (threads, correlations, calendar) whole.
    ctx_cap = 8 if local_mode else 12

    # Health (WHOOP) is fetched on its OWN track (see _fetch_health_chunks) and
    # passed in here, NOT mined out of the capped day-context bundle — on a busy
    # day the recovery read sits dozens of chunks deep in that bundle and the cap
    # drops it. Still strip any whoop chunk that slipped into the generic context
    # so it isn't printed twice. Empty health_chunks (source inactive) → the
    # HEALTH block renders nothing.
    other_ctx = [c for c in (context_chunks or []) if (c.get("source") or "") != "whoop"]
    health_rows = [r for c in (health_chunks or []) if (r := _vision_chunk_row(c))]
    ctx_rows = [r for c in other_ctx[:ctx_cap] if (r := _vision_chunk_row(c))]

    # Pre-grouped candidate links: each near-term event with the chunks a
    # semantic search judged related. The model decides whether the link is real.
    corr_blocks: list[dict] = []
    for corr in event_correlations or []:
        rows = [r for c in corr.get("chunks", []) if (r := _vision_chunk_row(c))]
        if rows:
            corr_blocks.append(
                {
                    "event": corr.get("event", ""),
                    "when_label": corr.get("when_label", ""),
                    "rows": rows,
                }
            )

    return {
        "calendar": _sanitise_action_titles(calendar),
        "overdue": _sanitise_action_titles(overdue),
        "today_rem": _sanitise_action_titles(today_rem),
        "wa_blocks": wa_blocks,
        "threads": threads,
        "ctx_rows": ctx_rows,
        "health_rows": health_rows,
        "corr_blocks": corr_blocks,
    }


def _build_vision_prompt(
    date_str: str,
    calendar: list[dict] | None = None,
    reminders: list[dict] | None = None,
    wa_chunks: list[dict] | None = None,
    context_chunks: list[dict] | None = None,
    health_chunks: list[dict] | None = None,
    news_digest: str = "",
    day_anchor: str = "",
    event_correlations: list[dict] | None = None,
    weather: str = "",
    chained: list[dict] | None = None,
    work_location: str = "",
    extracted_facts: dict | None = None,
    critic_feedback: str = "",
    local_mode: bool = False,
    rows: dict | None = None,
    deadline_lines: list[str] | None = None,
) -> str:
    """Build the day-brief HTML synthesis prompt fed to the LLM.

    ``critic_feedback`` (non-empty only on a repair pass) injects the previous
    draft's flagged problems so the model produces a corrected new draft.
    ``local_mode`` tells the template a small local model is composing — it
    then appends a worked output skeleton (a 14B follows an example far more
    reliably than forty rules). Pass EITHER ``rows`` (pre-assembled via
    :func:`_assemble_vision_rows` — the raw-chunk params are then unused) OR
    the raw chunk lists for in-place assembly; ``deadline_lines`` (mined in
    code) render as untouchable verbatim facts."""
    if rows is None:
        rows = _assemble_vision_rows(
            date_str,
            calendar or [],
            reminders or [],
            wa_chunks or [],
            context_chunks,
            health_chunks,
            event_correlations,
            extracted_facts,
            local_mode,
        )

    news_cap = 2600 if local_mode else 4000

    return render_prompt(
        "knowledge_day_vision",
        date_str=date_str,
        day_anchor=day_anchor,
        calendar=rows["calendar"],
        overdue=rows["overdue"],
        today_rem=rows["today_rem"],
        wa_blocks=rows["wa_blocks"],
        threads=rows["threads"],
        context_chunks=rows["ctx_rows"],
        health_chunks=rows["health_rows"],
        event_correlations=rows["corr_blocks"],
        weather=weather,
        chained=chained or [],
        work_location=(work_location or "").strip(),
        news_digest=(news_digest or "").strip()[:news_cap],
        extracted_facts=extracted_facts or {},
        user_context=runtime.user_context,
        language=runtime.language,
        critic_feedback=critic_feedback,
        local_mode=local_mode,
        deadline_lines=deadline_lines or [],
    )


# ── Briefing swarm: structured pre-pass + post-generation critic ────────────────

# A cheap, fast model extracts structured facts from the calendar + reminders
# before the main vision call, and a second cheap pass critiques the generated
# briefing against a checklist of known mistakes. Both wrap the LLM call passed
# in by the orchestrator (this module stays free of provider/DB knowledge) and
# never raise — a failure returns the safe default so the pipeline is unaffected.

# Async LLM callable: ``(prompt) -> output text``. Injected by the caller so it
# carries the provider/model and metric accounting.
LlmCall = Callable[[str], Awaitable[str]]

_EXTRACT_DEFAULTS: dict = {
    "physical_activities": [],
    "partner_events": [],
    "open_loops": [],
    "high_priority_reminders": [],
}

_CRITIC_DEFAULTS: dict = {"issues": [], "approved": True}

# JSON schemas mirroring the shapes the extractor / critic templates ask for.
# Passed as ``json_schema`` to the LLM call so the LOCAL provider decodes under
# a grammar that cannot produce anything else — a 14B model drifts out of a
# JSON shape that instructions alone hold a cloud model to. claude-cli ignores
# them. Keep in sync with ``briefing_extractor.j2`` / ``briefing_critic.j2``
# and the ``_EXTRACT_DEFAULTS`` / ``_CRITIC_DEFAULTS`` parse defaults above.
_TIMED_TITLE_ITEM = {
    "type": "object",
    "properties": {"when": {"type": "string"}, "title": {"type": "string"}},
    "required": ["title"],
}
EXTRACTOR_JSON_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "physical_activities": {"type": "array", "maxItems": 6, "items": _TIMED_TITLE_ITEM},
        "partner_events": {"type": "array", "maxItems": 6, "items": _TIMED_TITLE_ITEM},
        "open_loops": {"type": "array", "maxItems": 6, "items": {"type": "string"}},
        "high_priority_reminders": {
            "type": "array",
            "maxItems": 4,
            "items": {"type": "string"},
        },
    },
    "required": ["physical_activities", "partner_events", "open_loops", "high_priority_reminders"],
}
# Schema for the per-source summarisation / consolidation passes. The
# templates ask for a bare JSON array, but the grammar wraps it in
# ``{"items": [...]}`` (an object root is the shape llama.cpp's JSON mode is
# happiest with) — ``_parse_bullets`` already unwraps both forms.
BULLETS_JSON_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            # Bounded both ways. maxItems: an unbounded array lets a greedy
            # local decode enumerate items until it slams into max_tokens — and
            # a truncated reply is unparseable. minItems: without it the model
            # can close the array immediately — on long transcripts the
            # grammar-legal `]` outranks starting an item, and the reply
            # collapses to {"items":[]} (observed on a 17k-char transcript).
            # The summariser is only ever called on non-empty material, and the
            # templates ask for 1-5 items, so forcing ≥1 is safe.
            "minItems": 1,
            "maxItems": 6,
            "items": {
                "type": "object",
                "properties": {
                    "kind": {"type": "string", "enum": sorted(_ITEM_KINDS)},
                    "text": {"type": "string"},
                    "source": {"type": "string"},
                    "date": {"type": "string"},
                },
                "required": ["kind", "text"],
            },
        }
    },
    "required": ["items"],
}
# GBNF grammar for the day-vision's output shape, used by the LOCAL provider.
# It makes the structural contract physically unbreakable: an optional
# READINESS line, an OBJECTIVE line, prose-only paragraphs (no line may start
# with a bullet or heading marker), then AROUND with optional bullets that
# each END in a [src: …] attribution. A 14B keeps drifting into hour-by-hour
# bullet lists however firmly the prompt forbids them; under this grammar the
# drift cannot be expressed. Content quality stays on the prompt + critic —
# the grammar owns only the shape. Kept in sync with ``vision_lint`` (which
# still guards the cloud / grammar-off paths).
# The body/around transition must be UNAMBIGUOUS: if a prose line were
# allowed to start with "AROUND:", the parser would happily absorb the
# model's own AROUND section as more body — the grammar's real around-state
# is then never entered, EOS stays masked (root is incomplete), and the
# model, unable to stop, degenerates into a repetition loop until
# max_tokens (observed). Hence the unrolled `line` alternatives, which
# exclude exactly the uppercase "AROUND:" prefix from prose line starts —
# "Au cours…", "Avec…", even "AROUND the corner" all stay legal.
VISION_GBNF: str = r"""
root ::= readiness objective body "\n\n" around
readiness ::= ("READINESS: " text "\n\n")?
objective ::= "OBJECTIVE: " text "\n\n"
body ::= para ("\n\n" para)*
para ::= line ("\n" line)*
line ::= [^\n0-9A#•-] rest? | "A" notr rest? | "AR" noto rest? | "ARO" notu rest? | "AROU" notn rest? | "AROUN" notd rest? | "AROUND" [^:\n] rest? | digits (notpunct rest?)?
notr ::= [^\nR]
noto ::= [^\nO]
notu ::= [^\nU]
notn ::= [^\nN]
notd ::= [^\nD]
digits ::= [0-9]+
notpunct ::= [^.)\n]
rest ::= [^\n]+
text ::= [^\n]+
around ::= "AROUND: " text "\n" bullet{0,8}
bullet ::= "- " btext " [src: " stext "]" "\n"
btext ::= [^\n\[]{10,180}
stext ::= [^\n\]]{3,60}
"""
# bullet{0,8}: after each bullet the model chooses "another bullet" or EOS —
# and, unbounded, it never chooses EOS: it inventoried every context chunk
# (33 bullets, trivia included) until max_tokens cut it mid-line. Emission
# order tracks importance, so a hard cap keeps the strongest items.
# digits/notpunct: with "- " bullets blocked, the model fell back to numbered
# lists ("1. La matinée…"); a digit-led line is legal prose only when the
# digits aren't followed by "." or ")" — "9h45 …" passes, "1. …" cannot.
# btext{10,180}: terse periphery by construction — v4's bullets ballooned
# with empty clauses ("à noter pour une coordination ultérieure"); 180 chars
# fits one fact + its stake and nothing else. stext{3,60} keeps the [src: …]
# label from swallowing chunk metadata.

CRITIC_JSON_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "issues": {
            "type": "array",
            "maxItems": 8,
            "items": {
                "type": "object",
                "properties": {"type": {"type": "string"}, "excerpt": {"type": "string"}},
                "required": ["type"],
            },
        },
        "approved": {"type": "boolean"},
    },
    "required": ["issues", "approved"],
}

# Fact-critic verdict: closed issue types under the grammar (a 14B can't
# invent new ones), and an ``evidence`` field carrying the verbatim data span
# that contradicts the draft — the repair pass acts on the contradiction, not
# on a vague "something is inverted".
FACT_CRITIC_JSON_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "issues": {
            "type": "array",
            "maxItems": 6,
            "items": {
                "type": "object",
                "properties": {
                    "type": {
                        "type": "string",
                        "enum": [
                            "relation_inverted",
                            "status_polarity_inverted",
                            "fact_misattributed",
                            "unsupported_claim",
                        ],
                    },
                    "excerpt": {"type": "string"},
                    "evidence": {"type": "string"},
                },
                "required": ["type", "excerpt"],
            },
        },
        "approved": {"type": "boolean"},
    },
    "required": ["issues", "approved"],
}


async def extract_day_facts(
    date_str: str,
    calendar: list[dict],
    reminders: list[dict],
    llm: LlmCall,
) -> dict:
    """Light structured pre-pass: pull facts from calendar + reminders as JSON.

    ``llm`` is an async ``(prompt) -> text`` callable bound to the cheap
    extractor model by the orchestrator. Returns a dict shaped like
    :data:`_EXTRACT_DEFAULTS`; on any error (LLM failure or unparseable reply)
    it returns those defaults so the main briefing pass is never blocked.
    """
    facts = dict(_EXTRACT_DEFAULTS)
    if not calendar and not reminders:
        return facts
    try:
        prompt = render_prompt(
            "briefing_extractor",
            date_str=date_str,
            calendar=_sanitise_action_titles(calendar),
            reminders=_sanitise_action_titles(reminders),
        )
        payload = _extract_json_payload(await llm(prompt))
        if isinstance(payload, dict):
            facts.update({k: payload[k] for k in _EXTRACT_DEFAULTS if k in payload})
    except Exception as exc:  # noqa: BLE001 — non-blocking by contract
        log.warning("extract_day_facts failed, using defaults: %s", exc)
    return facts


def format_critic_feedback(issues: list[dict]) -> str:
    """Render critic issues as a directive list to inject into a repair pass.

    Each issue is ``{type, excerpt}`` from :func:`critique_briefing` (the
    fact-critic adds ``evidence`` — the verbatim data span that contradicts
    the draft); we turn it into a short imperative line the day-vision prompt
    can act on."""
    lines: list[str] = []
    for issue in issues or []:
        kind = str(issue.get("type") or "issue").replace("_", " ")
        excerpt = str(issue.get("excerpt") or "").strip()
        evidence = str(issue.get("evidence") or "").strip()
        if excerpt and evidence:
            lines.append(
                f'- {kind}: fix this — "{excerpt[:200]}" '
                f'(the data actually says: "{evidence[:200]}")'
            )
        elif excerpt:
            lines.append(f'- {kind}: fix this — "{excerpt[:200]}"')
        else:
            lines.append(f"- {kind}")
    return "\n".join(lines)


# Fact-critic data budget. The vision rows re-rendered for verification must
# leave room for the draft + instructions inside the local window; when the
# packed blocks overflow, drop the lowest-VALUE-to-the-critic blocks first
# (links → ctx → wa) — calendar, reminders and threads always survive, and the
# WhatsApp block goes LAST because it is the evidence that most often
# contradicts a draft (a message saying an event was cancelled/moved is exactly
# what the critic needs to catch a stale claim). Dropping it first blinded the
# critic to the one source that could disprove the writer.
FACT_PACK_MAX_CHARS = 10_000
_FACT_CAPS = {"threads": 4, "thread_rows": 4, "links": 6, "link_rows": 2, "ctx": 6, "wa_msgs": 2}


def _fact_pack_rows(rows: dict) -> dict:
    """Cap the vision rows down to the fact-critic's budget."""
    threads = [
        {**t, "rows": (t.get("rows") or [])[: _FACT_CAPS["thread_rows"]]}
        for t in (rows.get("threads") or [])[: _FACT_CAPS["threads"]]
    ]
    links = [
        {**c, "rows": (c.get("rows") or [])[: _FACT_CAPS["link_rows"]]}
        for c in (rows.get("corr_blocks") or [])[: _FACT_CAPS["links"]]
    ]
    wa_blocks = [
        {**b, "texts": [t[:300] for t in (b.get("texts") or [])[-_FACT_CAPS["wa_msgs"] :]]}
        for b in rows.get("wa_blocks") or []
    ]
    # Surface the cancellation flag to the fact-critic: the guard tagged these
    # events cancelled, but the template only renders the title, so fold the
    # marker into the title the critic reads. It backs the CANCELLATION RULE in
    # briefing_fact_critic.j2 with the deterministic signal.
    calendar = [
        ({**a, "title": f"{a.get('title') or ''} [ANNULÉ]"} if a.get("cancelled") else a)
        for a in rows.get("calendar") or []
    ]
    packed = {
        "calendar": calendar,
        "overdue": rows.get("overdue") or [],
        "today_rem": rows.get("today_rem") or [],
        "threads": threads,
        "links": links,
        "ctx_rows": (rows.get("ctx_rows") or [])[: _FACT_CAPS["ctx"]],
        "wa_blocks": wa_blocks,
    }

    def _size(p: dict) -> int:
        return sum(len(str(v)) for v in p.values())

    for droppable in ("links", "ctx_rows", "wa_blocks"):
        if _size(packed) <= FACT_PACK_MAX_CHARS:
            break
        packed[droppable] = []
    return packed


async def fact_critique_briefing(
    briefing_text: str,
    rows: dict,
    date_str: str,
    llm: LlmCall,
) -> dict:
    """Verify the draft's factual claims against the data the writer saw.

    The structural critic checks known mistake patterns against the calendar
    alone; this one re-reads the (capped) vision rows and hunts the local
    writer's quiet failures — inverted relations, flipped statuses,
    misattributed facts. ``llm`` is bound to the critic model by the
    orchestrator. Returns the :data:`_CRITIC_DEFAULTS` shape on any failure
    so the pipeline is never blocked.
    """
    result = dict(_CRITIC_DEFAULTS)
    if not (briefing_text or "").strip() or not rows:
        return result
    try:
        prompt = render_prompt(
            "briefing_fact_critic",
            date_str=date_str,
            briefing_text=briefing_text,
            **_fact_pack_rows(rows),
        )
        payload = _extract_json_payload(await llm(prompt))
        if isinstance(payload, dict):
            issues = payload.get("issues")
            result["issues"] = issues if isinstance(issues, list) else []
            result["approved"] = bool(payload.get("approved", not result["issues"]))
        else:
            # Empty/unparseable reply — the fact critic ran but said nothing
            # usable. Flag it so an outage is visible rather than passing as an
            # unchecked "approved".
            result["critic_error"] = "unparseable"
            log.warning("fact_critique_briefing: reply not a dict — treating as unavailable")
    except Exception as exc:  # noqa: BLE001 — non-blocking by contract
        result["critic_error"] = str(exc)[:120] or "exception"
        log.warning("fact_critique_briefing failed, using defaults: %s", exc)
    return result


async def critique_briefing(
    briefing_text: str,
    calendar: list[dict],
    partner_name: str,
    llm: LlmCall,
) -> dict:
    """Post-generation critic: check the briefing against a known-error checklist.

    ``llm`` is an async ``(prompt) -> text`` callable bound to the cheap critic
    model. Returns a dict shaped like :data:`_CRITIC_DEFAULTS`; on any error it
    returns those defaults (``approved`` true, no issues) so the pipeline is
    never blocked. The orchestrator decides what to do with the issues.
    """
    result = dict(_CRITIC_DEFAULTS)
    if not (briefing_text or "").strip():
        return result
    calendar_summary = [
        {
            "when": ev.get("when") or "All day",
            # Untrusted calendar titles — sanitize before they enter the critic prompt.
            "title": sanitize_chunk(ev.get("title") or ""),
            "type": ev.get("group_type") or "",
        }
        for ev in calendar
    ]
    try:
        prompt = render_prompt(
            "briefing_critic",
            briefing_text=briefing_text,
            calendar_summary=calendar_summary,
            partner_name=(partner_name or "").strip(),
        )
        payload = _extract_json_payload(await llm(prompt))
        if isinstance(payload, dict):
            issues = payload.get("issues")
            result["issues"] = issues if isinstance(issues, list) else []
            result["approved"] = bool(payload.get("approved", not result["issues"]))
        else:
            # Empty/unparseable critic reply (outage, truncation) — the critic
            # ran but said nothing usable. Record it so the run can surface an
            # advisory "critic unavailable" issue instead of silently reporting
            # an approved briefing that was never actually checked.
            result["critic_error"] = "unparseable"
            log.warning("critique_briefing: critic reply not a dict — treating as unavailable")
    except Exception as exc:  # noqa: BLE001 — non-blocking by contract
        result["critic_error"] = str(exc)[:120] or "exception"
        log.warning("critique_briefing failed, using defaults: %s", exc)
    return result
