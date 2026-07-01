"""Assemble the daily briefing note (title, dates, section headings, body) from the composed LLM outputs."""

from __future__ import annotations

import hashlib
import re
from datetime import date as _date

from estormi_briefing.lint.fact_lint import normalised_key

# Month names per UI language code. The briefing chrome (title, dates, section
# headings, footer) is localised to the `briefing_language` setting. The app
# ships French-only (the settings validator rejects any value but `fr`), so this
# resolves to `fr` at runtime; the `en` map is retained as a latent fallback (and
# exercised by tests) for a possible future bilingual edition.
_MONTHS_BY_LANG: dict[str, list[str]] = {
    "en": [
        "January",
        "February",
        "March",
        "April",
        "May",
        "June",
        "July",
        "August",
        "September",
        "October",
        "November",
        "December",
    ],
    "fr": [
        "janvier",
        "février",
        "mars",
        "avril",
        "mai",
        "juin",
        "juillet",
        "août",
        "septembre",
        "octobre",
        "novembre",
        "décembre",
    ],
}


def _norm_lang(lang: str) -> str:
    """Normalise a language code to one we have chrome strings for (`en`/`fr`)."""
    code = (lang or "").strip().lower()
    return code if code in _CHROME else "en"


def _long_date(date_str: str, lang: str = "en") -> str:
    """Long-form date in the briefing language.

    `en` → "June 5, 2026"; `fr` → "5 juin 2026". Falls back to the raw string
    when the input is not a parseable YYYY-MM-DD / YYYYMMDD date.
    """
    lang = _norm_lang(lang)
    raw = (date_str or "").strip()
    try:
        if re.fullmatch(r"\d{8}", raw):
            y, m, d = raw[:4], raw[4:6], raw[6:8]
        else:
            y, m, d = raw.split("-")[:3]
        month = _MONTHS_BY_LANG[lang][int(m) - 1]
        if lang == "en":
            return f"{month} {int(d)}, {y}"
        # French: the first of the month takes the ordinal "1er", others are bare.
        day_fr = "1er" if int(d) == 1 else str(int(d))
        return f"{day_fr} {month} {y}"
    except (ValueError, IndexError):
        return raw


# All briefing-chrome strings, keyed by language code then a stable slug.
# Format placeholders use str.format; `{date}` is the localised long date.
_CHROME: dict[str, dict[str, str]] = {
    "en": {
        "title": "Briefing — {date}",
        "readiness": "Readiness",
        "my_day": "My day",
        "around": "Around my day",
        "world": "The world",
        "todays_news": "Today's news",
        "themes": "Watch",
        "overdue": "Overdue",
        "schedule": "Schedule",
        "dont_forget": "Don't forget",
        "composed_by": "Composed by {model}",
        "composed_time": "at {time}",
        "sources_line": "Sources: {channels} channels, {counts} (last 24h)",
        "rss_count": "{n} RSS article(s)",
        "youtube_count": "{n} YouTube video(s)",
        "articles_count": "{n} new article(s)",
        "intro_none": "Briefing for {date}: nothing pressing detected right now.",
        "intro_prefix": "Briefing for {date}: ",
        "intro_overdue": "{n} overdue",
        "intro_appts": "{n} appointment(s)",
        "free_slot": "free slot",
        "all_day": "all day",
        "go_deeper": "Watch — go deeper",
        "overdue_short": "overdue",
        "overdue_since": "· {n}d",
    },
    "fr": {
        "title": "Briefing du {date}",
        "readiness": "Forme du jour",
        "my_day": "Ma journée",
        "around": "Autour de ma journée",
        "world": "Le monde",
        "todays_news": "L'actu du jour",
        "themes": "Veille",
        "overdue": "En retard",
        "schedule": "Au programme",
        "dont_forget": "À ne pas oublier",
        "composed_by": "Composé par {model}",
        "composed_time": "à {time}",
        "sources_line": "Sources : {channels} canaux, {counts} (dernières 24 h)",
        "rss_count": "{n} article(s) RSS",
        "youtube_count": "{n} vidéo(s) YouTube",
        "articles_count": "{n} nouvel(le)(s) article(s)",
        "intro_none": "Briefing du {date} : rien de pressant pour l'instant.",
        "intro_prefix": "Briefing du {date} : ",
        "intro_overdue": "{n} en retard",
        "intro_appts": "{n} rendez-vous",
        "free_slot": "créneau libre",
        "all_day": "toute la journée",
        "go_deeper": "Veille — aller plus loin",
        "overdue_short": "en retard",
        "overdue_since": "· depuis {n} j",
    },
}


def _t(lang: str, key: str, **kw: object) -> str:
    """Look up a localised chrome string and format it."""
    text = _CHROME[_norm_lang(lang)][key]
    return text.format(**kw) if kw else text


def briefing_title(date_str: str, lang: str = "en") -> str:
    """Localised briefing title (the JSON `title` field + the body <h1>)."""
    return _t(lang, "title", date=_long_date(date_str, lang))


def _esc(text: str) -> str:
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
# Single-asterisk italic, but only when the ``*`` hugs the word (no inner
# whitespace) and isn't adjacent to a word char or another ``*`` — so "2 * 3"
# and any ``**bold**`` leftover never match.
_ITALIC_RE = re.compile(r"(?<![\w*])\*(?!\s)([^*\n]+?)(?<!\s)\*(?![\w*])")


def _esc_with_md(text: str) -> str:
    """HTML-escape ``text`` then promote markdown emphasis to real tags:
    ``**foo**`` → ``<b>foo</b>``, ``*foo*`` → ``<i>foo</i>``.

    Weak local models emit markdown emphasis around titles and key phrases;
    without this the raw asterisks bleed through after ``_esc`` escapes them
    (Opus emits clean prose, Ministral does not). Any ``**`` left after
    pairing is an unclosed orphan (the model opened bold and never closed
    it) — drop it rather than ship literal asterisks.
    """
    out = _BOLD_RE.sub(r"<b>\1</b>", _esc(text))
    out = out.replace("**", "")
    out = _ITALIC_RE.sub(r"<i>\1</i>", out)
    # A lone ``*`` the model opened for italic and never closed survives the
    # pairing sub above. Strip it only when it trails the line (with optional
    # whitespace) — a flanked strip would eat the multiplication in "2 * 3".
    return re.sub(r"\s*\*\s*$", "", out)


# Leaked schema scaffolding: weak local models sometimes echo the JSON "kind"
# tag (news|insight|concept|important|prediction|fact) as a "[concept] …"
# prefix on a bullet or theme line. Strip it so it never reaches the reader.
# Matches a leaked schema tag — either bare ``[concept]`` or the wrapped
# ``[insight : free text]`` form some models emit as a title. The optional
# ``inner`` group is the text to keep when the tag wraps content.
_KIND_TAG_RE = re.compile(
    r"\[(?:news|insight|concept|important|prediction|fact)\s*(?::\s*(?P<inner>[^\]]*))?\]",
    re.IGNORECASE,
)


def _strip_kind_tag(text: str) -> str:
    """Drop a leaked ``[concept]`` tag, or unwrap ``[insight : foo]`` → ``foo``.

    These are JSON-schema artefacts weak local models echo into prose/titles;
    they can appear at the start of a bullet or mid-title (after an emoji).
    Remove/unwrap every occurrence and tidy the resulting whitespace."""
    cleaned = _KIND_TAG_RE.sub(lambda m: m.group("inner") or "", text or "")
    return re.sub(r"\s{2,}", " ", cleaned).strip()


def _norm_for_dedup(text: str) -> str:
    """Near-duplicate key (shared normaliser — see ``fact_lint.normalised_key``).

    Weak local models sometimes emit the same news item twice (verbatim or
    lightly reworded); this collapses the obvious repeats."""
    return normalised_key(text)


_CONTENT_WORD_RE = re.compile(r"[a-zà-ÿ0-9]{4,}", re.IGNORECASE)


def _content_tokens(text: str) -> set[str]:
    return {w.lower() for w in _CONTENT_WORD_RE.findall(text or "")}


def _token_containment(a: set[str], b: set[str]) -> float:
    """Share of the smaller block's vocabulary present in the other.

    Catches the paraphrase repeats the exact-key dedup misses: a weak model
    re-states the same theme three times in different words but the SAME
    vocabulary (Apple/App Intents/écosystème…) — containment ≥0.6 means the
    block adds nothing the reader hasn't just read."""
    if not a or not b:
        return 0.0
    return len(a & b) / min(len(a), len(b))


_THEME_NEAR_DUP_CONTAINMENT = 0.6


def _colored_heading(label: str, color: str) -> str:
    return f'<p style="color:{color}"><b>{_esc(label)}</b></p>'


def _section_heading(label: str, emoji: str) -> str:
    """A briefing section heading. Styling (Cinzel, gilt) lives in the shared
    briefing.css so iOS and macOS render it identically; only the emoji + text
    are emitted here."""
    return f"<h2>{_esc(emoji)} {_esc(label)}</h2>"


_DANGEROUS_BLOCK_RE = re.compile(
    r"<\s*(script|style)[^>]*>.*?<\s*/\s*\1\s*>",
    re.IGNORECASE | re.DOTALL,
)
# Trailing `[src: label]` / `[src: label · time]` attribution on a vision
# paragraph. One or more markers may be chained at the end of the paragraph;
# each is stripped and re-rendered as a gold source span. (Used to clean the
# readiness card.)
_VISION_ATTR_RE = re.compile(r"\s*\[src:\s*([^\]\n]+)\]\s*$", re.IGNORECASE)
# Same marker but matched ANYWHERE in a run, not just at the end — the model
# sometimes drops a `[src: …]` mid-sentence ("… le créneau. [src: mail] La
# machine …"); those must still render as a span, not leak as literal brackets.
_INLINE_ATTR_RE = re.compile(r"\[src:\s*([^\]\n]+)\]", re.IGNORECASE)
# A markdown horizontal rule (``---`` / ``***`` / ``___``) at the very start of
# a block — standalone or leading real prose. Weak models emit these as section
# separators; we drop the rule and keep any following text.
_HR_PREFIX_RE = re.compile(r"^[-*_]{3,}[ \t]*")
# A full LINE that is only a horizontal rule, anywhere in the text — the
# prefix rule above misses an ``---`` sandwiched between prose lines inside
# one block (it then gets joined into the paragraph as literal dashes).
_HR_LINE_RE = re.compile(r"^[ \t]*[-*_]{3,}[ \t]*$", re.MULTILINE)


def _source_attr(label: str) -> str:
    """Gold italic source attribution (' — mail · 06:12').

    Inline styles mirror the iOS `.source` CSS tokens so the attribution
    renders identically on the macOS modal, which carries no stylesheet for
    the briefing body.
    """
    return (
        f'<span class="source" style="color:#8A7142;font-size:0.85em;'
        f'font-style:italic"> — {_esc(label)}</span>'
    )


def _render_prose_with_attrs(text: str) -> str:
    """Escape + markdown a prose run, turning EVERY ``[src: …]`` marker — wherever
    it sits, paragraph-end or mid-sentence — into a gold source span. The text
    before each marker is right-trimmed so the span's leading " — " reads as a
    clean citation; a loose marker that this didn't catch would otherwise leak as
    literal brackets onto the page."""
    out: list[str] = []
    last = 0
    for m in _INLINE_ATTR_RE.finditer(text):
        out.append(_esc_with_md(text[last : m.start()].rstrip()))
        out.append(_source_attr(m.group(1).strip()))
        last = m.end()
    out.append(_esc_with_md(text[last:]))
    return "".join(out)


def _split_readiness(text: str) -> tuple[str, str]:
    """Peel a leading ``READINESS: …`` steer off the day-vision output.

    The day-vision is told to open with this sentinel line when WHOOP health
    data is present; we lift it into the readiness card at the top of the
    briefing and keep it out of the prose. Returns ``(readiness, remaining)``;
    ``("", text)`` when no sentinel is present (health source inactive, or a
    model that skipped it — graceful: no card, prose unchanged).
    """
    if not text:
        return "", text
    stripped = text.lstrip()
    # Tolerate a space before the colon ("READINESS :") and markdown the model
    # wraps the label in (``**READINESS:**``, ``# READINESS:``) — otherwise the
    # steer bleeds into the prose instead of lifting into the health card.
    m = re.match(r"[*#>\s-]*READINESS\s*\**\s*:\s*\**\s*", stripped, re.IGNORECASE)
    if not m:
        return "", text
    # The steer runs to the first blank line (paragraph break); the rest is prose.
    parts = re.split(r"\n\s*\n", stripped[m.end() :], maxsplit=1)
    readiness = " ".join(parts[0].split())
    remaining = parts[1] if len(parts) > 1 else ""
    return readiness, remaining


def _split_objective(text: str) -> tuple[str, str]:
    """Peel a leading ``OBJECTIVE: …`` line off the day-vision output.

    The day-vision opens MY DAY's prose with this sentinel naming the day's
    through-line; we lift it into the briefing subtitle under the title and keep
    it out of the prose. Returns ``(objective, remaining)``; ``("", text)`` when
    absent (graceful: no subtitle, prose unchanged). Tolerates markdown wrappers
    the model adds, mirroring :func:`_split_readiness`.
    """
    if not text:
        return "", text
    stripped = text.lstrip()
    m = re.match(r"[*#>\s-]*OBJECTIVE\s*\**\s*:\s*\**\s*", stripped, re.IGNORECASE)
    if not m:
        return "", text
    parts = re.split(r"\n\s*\n", stripped[m.end() :], maxsplit=1)
    objective = " ".join(parts[0].split())
    remaining = parts[1] if len(parts) > 1 else ""
    return objective, remaining


def _split_around(text: str) -> tuple[str, str]:
    """Split the day-vision on the ``AROUND:`` sentinel.

    Everything before it is the MY-DAY narrative; everything after is the
    periphery (orbiting items). Returns ``(my_day, around)``; ``(text, "")`` when
    the model omitted the sentinel (graceful: no Around section).
    """
    if not text:
        return text, ""
    parts = re.split(r"(?mi)^[*#>\s-]*AROUND\s*\**\s*:\s*\**\s*", text, maxsplit=1)
    if len(parts) < 2:
        return text, ""
    return parts[0].rstrip(), parts[1].strip()


# A world-corpus source tag on an AROUND bullet — news/RSS/YouTube items that
# belong in "Le monde", not the personal periphery. Personal labels (mail,
# gcal/agenda, whatsapp, reminder, notes, imessage, whoop, documents) are kept.
_AROUND_WORLD_SRC_RE = re.compile(r"\[src:\s*(?:news|rss|youtube|world)\b[^\]]*\]", re.IGNORECASE)


def _render_around_html(text: str) -> str:
    """Render the AROUND periphery: a linking intro (prose) then sourced bullets.

    Hybride layout (the user's choice): non-bullet lines form a short intro
    paragraph; ``- ``/``• `` lines become a sourced list, each peeling its
    trailing ``[src: …]`` into a gold attribution span like the news bullets.
    """
    intro_lines: list[str] = []
    bullets: list[str] = []
    seen: set[str] = set()
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("- ") or line.startswith("• "):
            bullet = line[2:].strip()
            # AROUND is the PERSONAL periphery (mail/chat/agenda look-ahead);
            # world news belongs in "Le monde". A model that drops a news item
            # here duplicates it across both sections (the canicule/Bolivie dupes
            # of 2026-06-21/-22), so skip bullets sourced from the world corpus.
            if _AROUND_WORLD_SRC_RE.search(bullet):
                continue
            # Drop near-duplicate orbit bullets (a weak model sometimes repeats
            # one, or the same chunk arrives under two prefixes). Mirror
            # _news_bullets_to_html so both lists dedup the same way.
            key = _norm_for_dedup(bullet)
            if key and key in seen:
                continue
            if key:
                seen.add(key)
            bullets.append(bullet)
        else:
            intro_lines.append(line)
    out: list[str] = []
    if intro_lines:
        intro = _render_vision_html("\n".join(intro_lines))
        if intro:
            out.append(intro)
    if bullets:
        out.append("<ul>")
        for b in bullets:
            li_html = _render_prose_with_attrs(_strip_kind_tag(b))
            if li_html.strip():
                out.append(f"<li>{li_html}</li>")
        out.append("</ul>")
    return "\n".join(out)


_MD_HEADER_RE = re.compile(r"^[ \t]*#{1,6}[ \t]*", re.MULTILINE)
# A standalone markdown horizontal rule ("---", "***", "___") a weak model drops
# between paragraphs. The HTML renderer swallows it, but the editable `fields`
# keep it raw (briefing_fields shares this cleaner) — strip it so both paths
# agree. Matches a whole line of 3+ of the same -, * or _ char.
_MD_HR_RE = re.compile(r"(?m)^[ \t]*([-*_])(?:[ \t]*\1){2,}[ \t]*$\n?")
# Standalone invented section titles a weak model echoes from the prompt
# structure (NOT user-meaningful headers like "Open loops:"). Dropped entirely.
_SCAFFOLD_LABEL_RE = re.compile(
    r"^(?:MORNING BRIEFING|DAY VISION|BRIEFING)\s*:?\s*$", re.IGNORECASE
)


def _strip_vision_scaffolding(text: str) -> str:
    """Remove day-vision section-header scaffolding a weak local model echoes.

    Strips leading markdown ``#`` headers, collapses a doubled ``READINESS:``
    label, and drops standalone invented section-title lines (e.g. a bare
    ``### MORNING BRIEFING:``). ``READINESS:`` is preserved — _split_readiness
    needs it to lift the health card. (Ministral-8B reproduced all three.)"""
    if not text:
        return text
    text = _MD_HEADER_RE.sub("", text)
    text = _MD_HR_RE.sub("", text)
    text = re.sub(r"(READINESS\s*:\s*)(?:READINESS\s*:\s*)+", r"\1", text, flags=re.IGNORECASE)
    kept = [ln for ln in text.splitlines() if not _SCAFFOLD_LABEL_RE.match(ln.strip())]
    return "\n".join(kept)


# Zone markers around the readiness card. The health refresh (see
# ``estormi_briefing.refresh_health``) regenerates ONLY this card when fresh
# WHOOP data lands after the morning briefing — the markers make the splice
# exact and unbreakable. Briefings built before the markers existed fall back
# to a structural regex on the card's div.
READINESS_MARK_START = "<!--readiness:start-->"
READINESS_MARK_END = "<!--readiness:end-->"

# Zone markers around the other two user-editable prose sections: the objective
# subtitle and the My-day narrative. Same contract as the readiness markers —
# they make the edit-time splice (the briefing PUT endpoint) exact, so
# correcting one section's prose never re-renders or disturbs the derived
# timeline / Around / World blocks. The HTML comments are element-invisible, so
# the ``.b-day > p:first-of-type`` drop cap still lands on the first narrative
# paragraph. Briefings built before the markers existed aren't field-editable
# (the editor falls back to the raw-HTML textarea).
OBJECTIVE_MARK_START = "<!--objective:start-->"
OBJECTIVE_MARK_END = "<!--objective:end-->"
MYDAY_MARK_START = "<!--myday:start-->"
MYDAY_MARK_END = "<!--myday:end-->"


def _readiness_card(text: str, lang: str = "en") -> str:
    """The health ``Readiness`` encart that opens the briefing.

    Built from ``<div>`` only — NOT ``<p>`` — so it doesn't steal the
    ``.b-day`` drop cap that lands on the day narrative's first paragraph. Inline
    styles mirror the iOS/web briefing CSS tokens (gilt on charbon) so the same
    HTML renders on every surface; both renderers inject ``htmlBody`` as raw HTML.
    The label is localised to the briefing language.
    """
    body = text
    # Drop any trailing [src: …] the model tacked on — the card stays chrome-free.
    while (m := _VISION_ATTR_RE.search(body)) is not None:
        body = body[: m.start()].rstrip()
    if not body:
        return ""
    label = _esc(_t(lang, "readiness"))
    return (
        f"{READINESS_MARK_START}"
        '<div class="readiness" style="background:rgba(196,154,58,0.07);'
        "border:1px solid #8A7142;border-left:3px solid #C49A3A;"
        'padding:12px 16px;margin:0 0 1.4em 0;border-radius:4px">'
        '<div style="color:#C8A96B;font-family:Cinzel,Georgia,serif;'
        'font-size:0.7em;letter-spacing:0.16em;text-transform:uppercase">'
        f"✦ {label}</div>"
        f'<div style="margin-top:0.45em;color:#F5F1E8">{_esc_with_md(body)}</div>'
        f"</div>{READINESS_MARK_END}"
    )


# Legacy (pre-marker) readiness card: the fixed two-inner-div shape emitted
# above, matched structurally for briefings built before the markers shipped.
_LEGACY_READINESS_RE = re.compile(r'<div class="readiness".*?</div></div>', re.DOTALL)
_MARKED_READINESS_RE = re.compile(
    re.escape(READINESS_MARK_START) + r".*?" + re.escape(READINESS_MARK_END), re.DOTALL
)


def splice_readiness_card(html_body: str, steer_text: str, lang: str = "en") -> str | None:
    """Replace the readiness card inside an assembled ``htmlBody``.

    ``steer_text`` is the bare steer (no ``READINESS:`` label). Returns the
    updated body, or ``None`` when no card could be located (the caller then
    falls back to a full regeneration).
    """
    new_card = _readiness_card(steer_text, lang)
    if not new_card:
        return None
    if _MARKED_READINESS_RE.search(html_body or ""):
        return _MARKED_READINESS_RE.sub(lambda _m: new_card, html_body, count=1)
    if _LEGACY_READINESS_RE.search(html_body or ""):
        return _LEGACY_READINESS_RE.sub(lambda _m: new_card, html_body, count=1)
    return None


def _render_vision_html(text: str) -> str:
    """Render the day-vision marker text into the editorial briefing HTML.

    The day-vision LLM emits prose, not HTML: paragraphs separated by blank
    lines, ``**bold**``/``*italic*`` emphasis, an optional ``> "quote" — attr``
    pull-quote, and trailing ``[src: label]`` attributions. Python builds every
    tag that reaches the page and ``_esc`` escapes the prose, so the briefing
    body cannot carry injected markup; ``_DANGEROUS_BLOCK_RE`` additionally drops
    any ``<script>``/``<style>`` block the model emits. Inline styles mirror
    the iOS CSS tokens so the same HTML renders on the macOS modal too.
    """
    text = _DANGEROUS_BLOCK_RE.sub("", text or "")
    text = _HR_LINE_RE.sub("", text)
    out: list[str] = []
    for block in re.split(r"\n\s*\n", text):
        block = block.strip()
        # Drop a markdown horizontal rule the model sprinkles between paragraphs,
        # whether standalone ("---") or leading a paragraph ("--- Saturday …").
        # Opus omits these; Ministral emits them and they'd render as literal
        # dashes (or an empty <p>) without this.
        block = _HR_PREFIX_RE.sub("", block).strip()
        if not block:
            continue
        if block.lstrip().startswith(">"):
            quote = " ".join(block.lstrip(">").split())
            if quote:
                out.append(
                    '<blockquote style="border-left:2px solid #8A7142;'
                    "padding:0.2em 0 0.2em 14px;margin:1.2em 0;"
                    'font-style:italic;color:rgba(245,241,232,0.85)">'
                    f"{_esc_with_md(quote)}</blockquote>"
                )
            continue
        para = " ".join(block.split())
        # Render every `[src: ...]` marker as a gold span, wherever it sits in the
        # paragraph (trailing or mid-sentence) — see _render_prose_with_attrs.
        para_html = _render_prose_with_attrs(para)
        if para_html.strip():
            out.append(f"<p>{para_html}</p>")
    return "\n".join(out)


def _objective_inner(text: str) -> str:
    """The objective subtitle's inner HTML (no zone markers) from plain text."""
    text = (text or "").strip()
    return f'<p class="briefing-objective">{_esc_with_md(text)}</p>' if text else ""


def _myday_inner(text: str) -> str:
    """The My-day narrative's inner HTML (no zone markers) from plain prose.

    Runs the same scaffolding strip + vision renderer the compose path uses, so
    a user edit produces markup identical to a freshly composed section."""
    text = _strip_vision_scaffolding(text or "")
    return _render_vision_html(text) if text.strip() else ""


def _marked(start: str, end: str, inner: str) -> str:
    """Wrap a section's inner HTML in its zone markers, or "" when empty."""
    return f"{start}{inner}{end}" if inner else ""


def _splice_marked(html_body: str, start: str, end: str, inner: str) -> str | None:
    """Replace the marked region ``start … end`` in ``html_body`` with ``inner``.

    Returns the updated body, or ``None`` when the markers are absent (the
    briefing predates them — the caller falls back to a raw-HTML edit)."""
    rx = re.compile(re.escape(start) + r".*?" + re.escape(end), re.DOTALL)
    if not rx.search(html_body or ""):
        return None
    replacement = f"{start}{inner}{end}"
    return rx.sub(lambda _m: replacement, html_body, count=1)


# Section name → (start marker, end marker, plain-text → inner-HTML renderer).
# Readiness splices through its own helper (it carries a localised label, so it
# needs the language); these two are language-independent.
_EDITABLE_SECTIONS = {
    "objective": (OBJECTIVE_MARK_START, OBJECTIVE_MARK_END, _objective_inner),
    "myDay": (MYDAY_MARK_START, MYDAY_MARK_END, _myday_inner),
}


def splice_section(html_body: str, name: str, text: str, lang: str = "en") -> str | None:
    """Re-render one editable section from plain text and splice it into the body.

    ``name`` is one of ``readiness`` | ``objective`` | ``myDay``. Returns the
    updated ``htmlBody`` or ``None`` when the section's markers can't be found
    (so the caller can report which fields it could not apply)."""
    if name == "readiness":
        return splice_readiness_card(html_body, text, lang)
    spec = _EDITABLE_SECTIONS.get(name)
    if spec is None:
        return None
    start, end, render = spec
    return _splice_marked(html_body, start, end, render(text))


def briefing_fields(vision_html: str) -> dict[str, str]:
    """Extract the plain-text source of each editable section from the day-vision.

    Mirrors the splits :func:`build_note` performs, so the stored fields are the
    exact strings the editor round-trips through :func:`splice_section`. Empty
    strings for sections the vision didn't carry."""
    cleaned = _strip_vision_scaffolding(vision_html or "")
    readiness, after_readiness = _split_readiness(cleaned)
    objective, after_objective = _split_objective(after_readiness)
    my_day_text, _around = _split_around(after_objective)
    return {
        "objective": objective.strip(),
        "readiness": readiness.strip(),
        "myDay": (my_day_text or "").strip(),
    }


# Palette for theme source colors — deterministic hash-based selection.
_THEME_PALETTE = [
    "#2563eb",  # blue
    "#16a34a",  # green
    "#7c3aed",  # violet
    "#d97706",  # orange
    "#db2777",  # pink
    "#0d9488",  # teal
    "#ea580c",  # amber-orange
    "#0891b2",  # cyan
]

_SOURCE_MARKER_RE = re.compile(r"\s*\[SOURCE:\s*([^\]]+)\]\s*$", re.IGNORECASE)
# Matches "SOURCE: label | title | date" lines (date and title are optional).
# Lenient: accepts 1 or 2 pipe separators so partial LLM output never leaks
# as raw text into the content paragraph.
_THEME_SOURCE_LINE_RE = re.compile(
    r"^SOURCE:\s*([^|\n]+?)(?:\|\s*([^|\n]*)(?:\|\s*(.+?))?)?$", re.IGNORECASE
)
# Any line starting with SOURCE: (catches non-matching variants to suppress them)
_THEME_SOURCE_PREFIX_RE = re.compile(r"^SOURCE:", re.IGNORECASE)
# Accept the current "THEME:" marker and the legacy French "THÈME:" so older
# briefings still parse; the prompt now emits "THEME:".
_THEME_HEADING_RE = re.compile(r"^TH[EÈ]ME:\s*(.+)$", re.IGNORECASE)


def _theme_source_color(label: str) -> str:
    """Deterministic color for a source label — hash into the palette.

    Uses SHA-1 because Python randomises the built-in ``hash()`` per process
    (PYTHONHASHSEED), which would give the same source label a different
    palette colour on every run and make the rendered briefing flicker.
    """
    # Not a security hash — just a stable bucket index into the palette.
    digest = hashlib.sha1(label.lower().strip().encode("utf-8"), usedforsecurity=False).hexdigest()
    idx = int(digest, 16) % len(_THEME_PALETTE)
    return _THEME_PALETTE[idx]


def _source_span(sources_raw: str, lang: str = "en") -> str:
    """Render a 'Source A · Source B | YYYY-MM-DD' string as colored inline spans.

    An optional trailing ``| date`` (the news item's publication date) is rendered
    as a muted date after the source names — the pipe separator keeps it
    unambiguous from the ``·``-separated source list. The date is localised.
    """
    sources_part, _, date_raw = sources_raw.partition("|")
    sources = [s.strip() for s in sources_part.split("·") if s.strip()]
    parts = []
    for src in sources:
        color = _theme_source_color(src)
        parts.append(f'<span style="color:{color};font-size:0.85em">{_esc(src)}</span>')
    span = " · ".join(parts)
    date_long = _long_date(date_raw.strip(), lang) if date_raw.strip() else ""
    if date_long:
        span += f' <span style="color:#6b7280;font-size:0.8em">· {_esc(date_long)}</span>'
    return span


def _sane_theme_date(raw: str, briefing_date: str) -> str:
    """Keep a SOURCE-line date only when it is plausibly the item's real date.

    The model writes these dates itself and drifts (a 2026 video stamped
    "2024-06-10"). The items are all from the trailing days, so: a date within
    45 days before the briefing day passes; otherwise retry with the briefing
    year (the classic wrong-year slip); still out → no date (omitting beats a
    wrong one). With no briefing date to judge against, pass through."""
    raw = (raw or "").strip()
    if not raw or not briefing_date:
        return raw
    try:
        day0 = _date.fromisoformat(briefing_date[:10])
        d = _date.fromisoformat(raw[:10])
    except ValueError:
        return ""
    if 0 <= (day0 - d).days <= 45:
        return d.isoformat()
    try:
        d = d.replace(year=day0.year)
    except ValueError:
        return ""
    return d.isoformat() if 0 <= (day0 - d).days <= 45 else ""


def _render_themes_html(text: str, lang: str = "en", date_str: str = "") -> str:
    """Parse the structured THEME:/SOURCE: text from the LLM and render as HTML.

    Each source block renders as: content paragraph → source span → spacing.

    Expected input format (produced by _themes_prompt):
        THEME: 🤖 Artificial Intelligence
        SOURCE: Channel A | "Title" | 2026-05-15
        Content summary...

        SOURCE: Channel B | "Other title" | 2026-05-15
        Channel B summary...

        THEME: 💰 Cryptocurrencies
        ...
    """
    out: list[str] = []
    # Deferred SOURCE-span parts ({label, episode, date_long, color}) — the
    # span is built at flush time so the episode field can be checked against
    # the content block it captions (a weak model fills it with a paraphrase
    # of its own summary; the reader then reads the same text twice).
    pending_source: dict | None = None
    content_lines: list[str] = []
    seen_blocks: set[str] = set()
    seen_tokens: list[set[str]] = []
    # One block per (theme, source): the prompt asks for ONE summary per
    # source under a theme; a weak local model re-states it three times in
    # different words (below any vocabulary-containment threshold) — the cap
    # is the deterministic enforcement of the editorial contract.
    current_theme = ""
    seen_theme_source: set[tuple[str, str]] = set()
    skip_block = False
    # In a structured section (it has SOURCE: lines), a content block with NO
    # source attribution is model scaffolding ("Détails techniques clés…"),
    # never reader content.
    structured = bool(re.search(r"(?mi)^\s*SOURCE:", text or ""))

    def _source_span_html(meta: dict, block_text: str) -> str:
        """Build the SOURCE span, dropping an episode that captions nothing.

        The middle field of ``SOURCE: label | "Title" | date`` is meant to be
        the episode/article TITLE. A weak model fills it with a multi-sentence
        paraphrase of the summary it just wrote — the Gemma bench rendered
        every veille block twice. Two provable signals kill it: a length no
        real title has, or a vocabulary containment with the content block
        high enough that the reader learns nothing from it.
        """
        episode = meta["episode"]
        if episode and len(episode) > 140:
            episode = ""
        if episode and block_text:
            containment = _token_containment(_content_tokens(episode), _content_tokens(block_text))
            if containment >= _THEME_NEAR_DUP_CONTAINMENT:
                episode = ""
        meta_parts = [f"<b>{_esc(meta['label'])}</b>"]
        if episode:
            meta_parts.append(f"“{_esc(episode)}”")
        if meta["date_long"]:
            meta_parts.append(_esc(meta["date_long"]))
        return (
            f'<p><span style="color:{meta["color"]};font-size:0.85em">'
            f"{' · '.join(meta_parts)}</span></p>"
        )

    def _flush_block() -> None:
        """Emit buffered content, then the source span, then inter-block spacing.

        Near-duplicate content blocks are dropped — exact repeats by key, and
        paraphrase repeats by vocabulary containment (a weak local model
        re-states the same theme several times in different words).
        """
        nonlocal pending_source
        text_block = ""
        if content_lines:
            text_block = " ".join(content_lines).strip()
            content_lines.clear()
            if pending_source is None and structured:
                return  # unattributed scaffolding in a sourced section
            key = _norm_for_dedup(text_block)
            tokens = _content_tokens(text_block)
            near_dup = key in seen_blocks or any(
                _token_containment(tokens, prev) >= _THEME_NEAR_DUP_CONTAINMENT
                for prev in seen_tokens
            )
            if text_block and not near_dup:
                seen_blocks.add(key)
                seen_tokens.append(tokens)
                out.append(f"<p>{_esc_with_md(text_block)}</p>")
            else:
                pending_source = None  # duplicate: drop its source line too
        if pending_source:
            out.append(_source_span_html(pending_source, text_block))
            out.append("<p>&nbsp;</p>")
            pending_source = None

    for raw_line in (text or "").splitlines():
        line = raw_line.strip()
        # A content line opening on a horizontal rule ("--- Détails techniques
        # clés (pour NateBJones) : …") is model scaffolding addressed to the
        # pipeline, never reader content — drop the whole line.
        if _HR_PREFIX_RE.match(line):
            continue

        heading_m = _THEME_HEADING_RE.match(line)
        source_m = _THEME_SOURCE_LINE_RE.match(line)

        if heading_m:
            _flush_block()
            # Theme titles arrive with markdown bold half the time — the
            # heading is already <b>, so just drop the asterisks.
            title = _strip_kind_tag(heading_m.group(1).strip()).replace("**", "")
            current_theme = title.lower()
            skip_block = False
            out.append(f"<p><b>{_esc(title)}</b></p>")

        elif source_m:
            _flush_block()
            # Weak models echo the prompt's bracketed placeholders: "[label]"
            # for the source, a bullet kind tag posing as the episode title,
            # and a guessed (often wrong-year) date.
            label = source_m.group(1).strip().strip("[]").strip()
            theme_source = (current_theme, label.lower())
            if theme_source in seen_theme_source:
                # Second/third block for the same source under the same theme —
                # the paraphrase repeat. Skip its content too.
                skip_block = True
                pending_source = None
                continue
            seen_theme_source.add(theme_source)
            skip_block = False
            episode = (source_m.group(2) or "").strip().strip('"').strip("«»").strip()
            episode = _strip_kind_tag(episode)
            if re.fullmatch(
                r"(?:news|insight|concept|important|prediction|fact)?", episode, re.IGNORECASE
            ):
                episode = ""
            date_raw = _sane_theme_date((source_m.group(3) or "").strip(), date_str)
            pending_source = {
                "label": label,
                "episode": episode,
                "date_long": _long_date(date_raw, lang) if date_raw else "",
                "color": _theme_source_color(label),
            }

        elif _THEME_SOURCE_PREFIX_RE.match(line):
            # SOURCE: line that didn't match the full regex (e.g. no pipes at all).
            # Suppress it — never render a raw SOURCE: line as content text.
            _flush_block()

        elif line:
            if skip_block:
                continue
            content_lines.append(_strip_kind_tag(line))

        else:  # blank line between blocks
            _flush_block()

    _flush_block()
    return "\n".join(out)


def _news_bullets_to_html(text: str, lang: str = "en") -> list[str]:
    """Render LLM news bullets with inline colored source attribution.

    Parses the [SOURCE: ...] marker appended by the LLM and converts it to a
    styled inline span after the bullet text. Dates in the span are localised.
    """
    out = ["<ul>"]
    seen: set[str] = set()
    calendar_flag_used = False
    for raw in text.strip().splitlines():
        line = raw.strip()
        if not (line.startswith("- ") or line.startswith("• ")):
            continue
        content = _strip_kind_tag(line[2:].strip())
        m = _SOURCE_MARKER_RE.search(content)
        body_text = content[: m.start()].rstrip() if m else content
        # The 📅 schedule signal is meaningful on ONE item; a weak model
        # stamps it on half the section, which reads as decoration. Keep the
        # first, strip the rest (↩ Follow-up markers are never touched).
        if body_text.startswith("📅"):
            if calendar_flag_used:
                body_text = body_text.lstrip("📅").lstrip()
            calendar_flag_used = True
        # Drop near-duplicate items (weak local models sometimes repeat one).
        key = _norm_for_dedup(body_text)
        if not key or key in seen:
            continue
        seen.add(key)
        if m:
            body = _esc_with_md(body_text)
            source_html = _source_span(m.group(1), lang)
            out.append(f"<li>{body} {source_html}</li>")
        else:
            # Attribution is now carried in CODE: ``resolve_news_citations`` drops
            # every uncited bullet and ``fallback_news_from_items`` re-attaches the
            # input's own ``[SOURCE:]``, so a markerless bullet reaching here is an
            # anomaly. Render it plainly rather than leak a "(source?)" hint into
            # the shipped briefing.
            out.append(f"<li>{_esc_with_md(content)}</li>")
    out.append("</ul>")
    return out


def _dont_forget_line(reminders: list[dict], lang: str = "en", cap: int = 6) -> str:
    """Code-rendered reminders line under "My day" — due-today first, then
    overdue. The prose no longer guarantees reminder coverage (it carries
    insights, not lists), so this deterministic line does; zero LLM words,
    zero hallucination surface.

    Due-today leads so a backlog of overdue chores can't fill the cap and evict
    what's actually due today — the one thing this line must never drop.

    Within the overdue tail: still-live items lead, ordered by recency of the
    due date (most-recently-overdue first) so the freshest slip reads first, and
    each carries a compact "· depuis N j" age affordance (R2). A reminder a
    recent message proves DONE (``resolved_evidence``) is DEMOTED out of the red
    urgency — rendered plain and listed last, never hidden (R1, no-soft-hide)."""
    today = [r for r in reminders if not r.get("overdue")]
    # Live overdue: most-recently-overdue first (smallest days_overdue leads).
    live_overdue = [r for r in reminders if r.get("overdue") and not r.get("resolved_evidence")]
    live_overdue.sort(key=lambda r: int(r.get("days_overdue") or 0))
    # Demoted overdue (completion-evidence): kept, listed last, no red urgency.
    done_overdue = [r for r in reminders if r.get("overdue") and r.get("resolved_evidence")]

    parts: list[str] = []
    for r in today + live_overdue + done_overdue:
        title = str(r.get("title") or "").strip()
        if not title:
            continue
        when = str(r.get("when") or "").strip()
        bit = _esc(title)
        if r.get("overdue") and not r.get("resolved_evidence"):
            since = ""
            days = int(r.get("days_overdue") or 0)
            if days > 0:
                since = " " + _esc(_t(lang, "overdue_since", n=days))
            bit = (
                f'<span style="color:#dc2626">⚠ {bit} '
                f"({_esc(_t(lang, 'overdue_short'))}){since}</span>"
            )
        elif when and when.lower() != "all day" and when != "00:00":
            # Midnight is how a date-only reminder renders its "time" — showing
            # "— 00:00" reads as a deadline that doesn't exist.
            bit = f"{bit} — {_esc(when)}"
        parts.append(bit)
        if len(parts) >= cap:
            break
    if not parts:
        return ""
    # French typography puts a space before the colon; English does not.
    label = _esc(_t(lang, "dont_forget")) + (" :" if _norm_lang(lang) == "fr" else ":")
    return (
        '<p class="b-reminders" style="font-size:0.92em">'
        f'<b style="color:#C8A96B">{label}</b> ' + " · ".join(parts) + "</p>"
    )


def _brief_intro(date_str: str, actions: dict, lang: str = "en") -> str:
    # Expired timed reminders are stale, not live: exclude them from the count
    # so the intro can't announce "5 en retard" for chores that already aged out
    # (mirrors the list build_note renders — see the ``expired`` flag).
    overdue_count = sum(
        1 for r in actions.get("reminders", []) if r.get("overdue") and not r.get("expired")
    )
    calendar_count = len(actions.get("calendar", []) or [])

    parts = []
    if overdue_count:
        parts.append(_t(lang, "intro_overdue", n=overdue_count))
    if calendar_count:
        parts.append(_t(lang, "intro_appts", n=calendar_count))

    date = _long_date(date_str, lang)
    if not parts:
        return _t(lang, "intro_none", date=date)
    return _t(lang, "intro_prefix", date=date) + ", ".join(parts) + "."


def build_note(
    date_str: str,
    source_count: int,
    video_count: int,
    actions: dict | None = None,
    vision_html: str = "",
    news_synthesis: str = "",
    themes_html: str = "",
    rss_articles: int | None = None,
    youtube_videos: int | None = None,
    model_label: str = "",
    composed_at: str = "",
    lang: str = "en",
    timeline_html: str = "",
) -> str:
    """Build the daily briefing HTML body.

    actions:        {calendar, reminders} from _fetch_daily_actions
    vision_html:    LLM-generated day-vision summary (READINESS:/OBJECTIVE:/AROUND:
                    sentinels + the MY DAY narrative)
    news_synthesis: cross-source news synthesis plain text; rendered in The world
    themes_html:    LLM-generated theme-clustered HTML for the watch
    rss_articles:   per-type breakdown (RSS articles fetched). When both this
                    and ``youtube_videos`` are provided the footer prints a
                    split count instead of the legacy combined ``video_count``.
    youtube_videos: per-type breakdown (YouTube videos summarised).
    lang:           briefing language code (``en``/``fr``) for all chrome strings.

    Layout (editorial path) is the five sections the UI is built around:
    title+objective · Readiness · My day · Around my day · The world.
    """
    actions = actions or {}
    lang = _norm_lang(lang)

    def _footer_counts_phrase() -> str:
        """Footer counter phrasing — split RSS vs YouTube when we have both."""
        if rss_articles is not None and youtube_videos is not None:
            return " · ".join(
                [
                    _t(lang, "rss_count", n=rss_articles),
                    _t(lang, "youtube_count", n=youtube_videos),
                ]
            )
        return _t(lang, "articles_count", n=video_count)

    def _footer() -> str:
        composed = f"<br>{_esc(_t(lang, 'composed_by', model=model_label))}" if model_label else ""
        sources = _t(lang, "sources_line", channels=source_count, counts=_footer_counts_phrase())
        # Generation time sits right after the date — "Briefing du 6 juin 2026 à 09:24".
        at_time = f" {_t(lang, 'composed_time', time=composed_at)}" if composed_at else ""
        return (
            f'<hr>\n<p class="b-footer"><i>Estormi — '
            f"{_esc(briefing_title(date_str, lang))}{_esc(at_time)}<br>"
            f"{_esc(sources)}{composed}</i></p>"
        )

    title_html = f'<h1 class="briefing-title">{_esc(briefing_title(date_str, lang))}</h1>'
    calendar = actions.get("calendar") or []
    # Drop expired timed reminders (a slot that passed >24h before day-start):
    # they're stale, not live errands, and only pad the overdue list. Date-only
    # chores never carry the flag, so they keep rolling forward (_is_expired).
    reminders = [r for r in (actions.get("reminders") or []) if not r.get("expired")]
    has_actions = bool(calendar or reminders)

    # Lift the READINESS:/OBJECTIVE: steers and split off the AROUND: periphery —
    # strip any section-header scaffolding a weak model echoed first.
    cleaned = _strip_vision_scaffolding(vision_html)
    readiness, after_readiness = _split_readiness(cleaned)
    objective, after_objective = _split_objective(after_readiness)
    my_day_text, around_text = _split_around(after_objective)
    my_day_html = _render_vision_html(my_day_text) if my_day_text else ""
    around_html = _render_around_html(around_text) if around_text else ""

    def _world_section() -> list[str]:
        body: list[str] = []
        if news_synthesis:
            body.extend(_news_bullets_to_html(news_synthesis, lang))
        if themes_html:
            # The per-channel watch blocks are depth, not the morning read:
            # they fold behind a <details> so the briefing ends light and the
            # reader opts into "go deeper" (WKWebView and browsers both render
            # <details> natively — no client change needed).
            themes_block = _render_themes_html(themes_html.strip(), lang, date_str)
            if themes_block:
                body.append(
                    '<details class="b-veille" style="margin-top:1.2em">'
                    '<summary style="cursor:pointer;color:#C8A96B;'
                    "font-family:Cinzel,Georgia,serif;font-size:0.85em;"
                    'letter-spacing:0.12em;text-transform:uppercase">'
                    f"📺 {_esc(_t(lang, 'go_deeper'))}</summary>"
                    f"{themes_block}</details>"
                )
        if not body:
            return []
        return [
            '<section class="b-world">',
            _section_heading(_t(lang, "world"), "🌍"),
            *body,
            "</section>",
        ]

    if my_day_html or readiness or objective:
        # Editorial path: the five value-oriented sections. The drop cap lands on
        # the first paragraph of "My day" (.b-day in the shared CSS).
        lines: list[str] = [title_html]
        if objective:
            lines.append(
                _marked(OBJECTIVE_MARK_START, OBJECTIVE_MARK_END, _objective_inner(objective))
            )
        if readiness:
            lines.append(_readiness_card(readiness, lang))
        if my_day_html or timeline_html:
            lines.append('<section class="b-day">')
            lines.append(_section_heading(_t(lang, "my_day"), "📅"))
            # Coverage lives in code: the timeline strip carries the bare
            # schedule, the reminders line carries what's due — the prose
            # above them only ever carries insight.
            if timeline_html:
                lines.append(timeline_html)
            if my_day_html:
                lines.append(_marked(MYDAY_MARK_START, MYDAY_MARK_END, my_day_html))
            dont_forget = _dont_forget_line(reminders, lang)
            if dont_forget:
                lines.append(dont_forget)
            lines.append("</section>")
        if around_html:
            lines.append('<section class="b-around">')
            lines.append(_section_heading(_t(lang, "around"), "🔭"))
            lines.append(around_html)
            lines.append("</section>")
        lines.extend(_world_section())
        lines.append(_footer())
        return "\n".join(lines)

    # Fallback structured layout — no day-vision was generated (no actions, or the
    # LLM call failed). Lead with the summary line, then News, Watch, My day lists.
    lines = [title_html, f"<p><b>{_esc(_brief_intro(date_str, actions, lang))}</b></p>"]

    if news_synthesis:
        lines.append("<hr>")
        lines.append(_section_heading(_t(lang, "todays_news"), "🗞️"))
        lines.extend(_news_bullets_to_html(news_synthesis, lang))

    if themes_html:
        lines.append("<hr>")
        lines.append(_section_heading(_t(lang, "themes"), "📺"))
        lines.append(_render_themes_html(themes_html.strip(), lang, date_str))

    if has_actions or timeline_html:
        lines.append("<hr>")
        lines.append(_section_heading(_t(lang, "my_day"), "📅"))
        # The deterministic schedule strip carries the day's coverage; it must
        # survive the fallback (prose failed) just as it does the editorial path,
        # so the reader never loses the bare schedule to a transient LLM outage.
        if timeline_html:
            lines.append(timeline_html)

        overdue = [r for r in reminders if r.get("overdue")]
        today_reminders = [r for r in reminders if not r.get("overdue")]
        overdue_label = _esc(_t(lang, "overdue"))

        if overdue:
            lines.append(_colored_heading(_t(lang, "overdue"), "#dc2626"))
            lines.append("<ul>")
            for a in overdue:
                when = _esc(str(a.get("when") or ""))
                title = _esc(str(a.get("title") or "(untitled)"))
                prefix = f"<b>{when}</b> — " if when else ""
                lines.append(
                    f'<li>⚠️ <span style="color:#dc2626"><b>{overdue_label}</b></span> — {prefix}{title}</li>'
                )
            lines.append("</ul>")

        if calendar:
            lines.append(_colored_heading(_t(lang, "schedule"), "#2563eb"))
            lines.append("<ul>")
            for a in calendar:
                when = _esc(str(a.get("when") or ""))
                title = _esc(str(a.get("title") or "(untitled)"))
                prefix = f"<b>{when}</b> — " if when else ""
                lines.append(f"<li>{prefix}{title}</li>")
            lines.append("</ul>")

        if today_reminders:
            lines.append(_colored_heading(_t(lang, "dont_forget"), "#16a34a"))
            lines.append("<ul>")
            for a in today_reminders:
                when = _esc(str(a.get("when") or ""))
                title = _esc(str(a.get("title") or "(untitled)"))
                prefix = f"<b>{when}</b> — " if when else ""
                lines.append(f"<li>{prefix}{title}</li>")
            lines.append("</ul>")

    lines.append(_footer())
    return "\n".join(lines)
