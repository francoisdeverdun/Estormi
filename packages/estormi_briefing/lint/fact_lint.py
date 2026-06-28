"""Deterministic date-fidelity lint for the day-vision output.

The local writer's worst failure is quiet: it moves a deadline to another
date or attaches it to another subject ("mail of 19 Mar about the 22 Jun
Firebase cutoff" became "Runner migration due 16 Jun"). A date is the one
kind of fact code can verify exactly: every date the draft mentions must
exist somewhere in the data that was shown to the model.

This module is pure (no LLM, no I/O), mirrors ``vision_lint``'s issue shape
``{"type", "excerpt"}``, and is deliberately conservative: it only extracts
explicit day+month pairs (never bare day numbers, clock times like ``9h45``
or ``14:00``, amounts, or lone years), and a numeric ``dd/mm`` is accepted
under either day/month order so locale ambiguity can never false-positive.

It also mines the data for explicit deadline sentences
(:func:`extract_deadline_lines`) so the vision prompt can show them verbatim
— prevention on top of detection.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from datetime import date, timedelta

_MONTHS = {
    # French
    "janvier": 1,
    "février": 2,
    "fevrier": 2,
    "mars": 3,
    "avril": 4,
    "mai": 5,
    "juin": 6,
    "juillet": 7,
    "août": 8,
    "aout": 8,
    "septembre": 9,
    "octobre": 10,
    "novembre": 11,
    "décembre": 12,
    "decembre": 12,
    # English full
    "january": 1,
    "february": 2,
    "march": 3,
    "april": 4,
    "may": 5,
    "june": 6,
    "july": 7,
    "august": 8,
    "september": 9,
    "october": 10,
    "november": 11,
    "december": 12,
    # English abbreviated (the form ``when_label``/[src: …] markers carry)
    "jan": 1,
    "feb": 2,
    "mar": 3,
    "apr": 4,
    "jun": 6,
    "jul": 7,
    "aug": 8,
    "sep": 9,
    "sept": 9,
    "oct": 10,
    "nov": 11,
    "dec": 12,
}

_MONTH_ALT = "|".join(sorted(_MONTHS, key=len, reverse=True))

_ISO_RE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
# "22 juin", "1er juin", "12 Jun" — day first, month word. \b after the month
# alternation keeps "mar" from matching inside "mardi"/"market".
_DAY_MONTH_RE = re.compile(rf"\b(\d{{1,2}})(?:er)?\s+({_MONTH_ALT})\b\.?", re.IGNORECASE)
# "Jun 22", "June 22" — month word first (English order).
_MONTH_DAY_RE = re.compile(rf"\b({_MONTH_ALT})\.?\s+(\d{{1,2}})\b", re.IGNORECASE)
# "16/06", "16/06/2026" — numeric; both day/month readings are candidates.
# Fractions and scores ("2/3 du budget", "victoire 2/1") share this shape, so
# a slash pair only counts as a date when something disambiguates it: a year
# suffix, a component too big to be a month, or a date keyword right before.
_NUMERIC_RE = re.compile(r"\b(\d{1,2})/(\d{1,2})(/\d{2,4})?\b")
_DATE_KEYWORD_BEFORE_RE = re.compile(
    r"(?i)\b(le|au|du|dès|avant le|jusqu'au|pour le|on|by|for|until)\s*$"
)

# A deadline-bearing sentence: an explicit deadline keyword (or English
# "by <month>") in the same sentence as a date.
_DEADLINE_KEYWORD_RE = re.compile(
    r"(?i)\b(échéance|date limite|avant le|d'ici|au plus tard|dernier délai|"
    r"deadline|due|expires?|no later than|jusqu'au|cutoff)\b"
    rf"|\bby\s+(?:the\s+)?(?:\d{{1,2}}\s+)?(?:{_MONTH_ALT})\b"
)
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+|\n+")

_WEEKDAYS = {
    "lundi": 0,
    "mardi": 1,
    "mercredi": 2,
    "jeudi": 3,
    "vendredi": 4,
    "samedi": 5,
    "dimanche": 6,
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
}
_WEEKDAY_ALT = "|".join(_WEEKDAYS)
# "lundi 16 juin" / "Friday 12 Jun" — a weekday claimed for an explicit date.
_WEEKDAY_DATE_RE = re.compile(
    rf"\b({_WEEKDAY_ALT})\s+(\d{{1,2}})(?:er)?\s+({_MONTH_ALT})\b", re.IGNORECASE
)

_MAX_DEADLINE_LINES = 6
_MAX_DEADLINE_CHARS = 200
_MAX_DATE_ISSUES = 3


def _valid(month: int, day: int) -> bool:
    return 1 <= month <= 12 and 1 <= day <= 31


def extract_date_mentions(text: str) -> list[tuple[str, frozenset[tuple[int, int]]]]:
    """Explicit day+month mentions in ``text`` — ``(context, candidates)``.

    ``candidates`` holds every (month, day) reading of the mention (numeric
    forms get both orders). ``context`` is the line the mention sits on, for
    the issue excerpt.
    """
    out: list[tuple[str, frozenset[tuple[int, int]]]] = []
    for line in (text or "").splitlines():
        for m in _ISO_RE.finditer(line):
            month, day = int(m.group(2)), int(m.group(3))
            if _valid(month, day):
                out.append((line, frozenset({(month, day)})))
        for m in _DAY_MONTH_RE.finditer(line):
            day, month = int(m.group(1)), _MONTHS[m.group(2).lower()]
            if _valid(month, day):
                out.append((line, frozenset({(month, day)})))
        for m in _MONTH_DAY_RE.finditer(line):
            month, day = _MONTHS[m.group(1).lower()], int(m.group(2))
            if _valid(month, day):
                out.append((line, frozenset({(month, day)})))
        for m in _NUMERIC_RE.finditer(line):
            a, b = int(m.group(1)), int(m.group(2))
            date_like = (
                m.group(3) is not None
                or max(a, b) > 12
                or _DATE_KEYWORD_BEFORE_RE.search(line[: m.start()]) is not None
            )
            if not date_like:
                continue
            candidates = {(mth, d) for mth, d in ((b, a), (a, b)) if _valid(mth, d)}
            if candidates:
                out.append((line, frozenset(candidates)))
    return out


def _iter_strings(node: object) -> Iterator[str]:
    """Every string value reachable inside a rows structure (dicts/lists)."""
    if isinstance(node, str):
        yield node
    elif isinstance(node, dict):
        for v in node.values():
            yield from _iter_strings(v)
    elif isinstance(node, (list, tuple)):
        for v in node:
            yield from _iter_strings(v)


def allowed_date_set(
    rows: object, news_digest: str = "", date_str: str = ""
) -> set[tuple[int, int]]:
    """Every (month, day) the data legitimately contains.

    Mines all string values of ``rows`` (when_labels, titles, texts — shape-
    agnostic so prompt-row evolution can't silently starve it), the news
    digest, and the briefing day itself plus the next two days (the day
    anchor names them, so "tomorrow"-style prose may cite them).
    """
    allowed: set[tuple[int, int]] = set()
    for text in _iter_strings(rows):
        for _, candidates in extract_date_mentions(text):
            allowed |= candidates
    for _, candidates in extract_date_mentions(news_digest or ""):
        allowed |= candidates
    if date_str:
        try:
            day0 = date.fromisoformat(date_str[:10])
            for off in range(3):
                d = day0 + timedelta(days=off)
                allowed.add((d.month, d.day))
        except ValueError:
            pass
    return allowed


def lint_dates(draft: str, allowed: set[tuple[int, int]]) -> list[dict]:
    """Flag draft dates that exist under NO reading in the data.

    Returns ``{"type": "date_not_in_data", "excerpt": …}`` issues in the
    critic shape, capped and deduplicated.
    """
    if not allowed:
        # No mineable dates at all — refuse to guess rather than flag everything.
        return []
    issues: list[dict] = []
    seen: set[frozenset[tuple[int, int]]] = set()
    for context, candidates in extract_date_mentions(draft or ""):
        if candidates & allowed or candidates in seen:
            continue
        seen.add(candidates)
        issues.append(
            {
                "type": "date_not_in_data",
                "excerpt": context.strip()[:160]
                + " — this date appears nowhere in the data; use only dates shown "
                "beside or inside the items, never move a deadline",
            }
        )
        if len(issues) >= _MAX_DATE_ISSUES:
            break
    return issues


# Year-bearing prose dates — "28 mai 2026", "March 22, 2027". Matched BEFORE
# the year-less forms: a mention with an explicit year is exact, and letting
# the year-less regex re-read "28 mai" out of "28 mai 2026" would resurrect a
# past date as next year's.
_DAY_MONTH_YEAR_RE = re.compile(
    rf"\b(\d{{1,2}})(?:er)?\s+({_MONTH_ALT})\.?\s+(\d{{4}})\b", re.IGNORECASE
)
_MONTH_DAY_YEAR_RE = re.compile(
    rf"\b({_MONTH_ALT})\.?\s+(\d{{1,2}}),?\s+(\d{{4}})\b", re.IGNORECASE
)


def nearest_future_date(text: str, today: date) -> date | None:
    """The earliest explicit date in ``text`` that falls on/after ``today``.

    The actionable angle of a fact is its NEXT deadline, not its headline date
    (a shutdown mail leads with "March 2027" while the real cutoff is "June
    22"). ISO and year-bearing mentions keep their exact year — a past "28 mai
    2026" must never resolve to 2027. Year-less mentions resolve to the first
    occurrence on/after ``today`` (this year, else next) — mirroring
    :func:`extract_date_mentions`'s patterns so both stay date-compatible.
    """
    text = text or ""
    best: date | None = None

    def _consider(year: int, month: int, day: int) -> None:
        nonlocal best
        try:
            candidate = date(year, month, day)
        except ValueError:
            return
        if candidate >= today and (best is None or candidate < best):
            best = candidate

    year_spans: list[tuple[int, int]] = []
    for m in _ISO_RE.finditer(text):
        year_spans.append(m.span())
        _consider(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    for m in _DAY_MONTH_YEAR_RE.finditer(text):
        year_spans.append(m.span())
        _consider(int(m.group(3)), _MONTHS[m.group(2).lower()], int(m.group(1)))
    for m in _MONTH_DAY_YEAR_RE.finditer(text):
        year_spans.append(m.span())
        _consider(int(m.group(3)), _MONTHS[m.group(1).lower()], int(m.group(2)))

    def _inside_year_span(start: int, end: int) -> bool:
        return any(start >= s and end <= e for s, e in year_spans)

    yearless: list[tuple[int, int]] = []
    for m in _DAY_MONTH_RE.finditer(text):
        if not _inside_year_span(*m.span()):
            yearless.append((_MONTHS[m.group(2).lower()], int(m.group(1))))
    for m in _MONTH_DAY_RE.finditer(text):
        if not _inside_year_span(*m.span()):
            yearless.append((_MONTHS[m.group(1).lower()], int(m.group(2))))
    for month, day in yearless:
        for year in (today.year, today.year + 1):
            try:
                candidate = date(year, month, day)
            except ValueError:
                continue
            if candidate >= today:
                _consider(year, month, day)
                break
    return best


# Unit-bearing figures — the kind a summary bullet must not invent. A number
# qualifies when it carries a unit (money/percent/measures) or is large enough
# (≥1000) to be a quantity rather than prose ("deux heures", "3 sujets" stay
# out of scope, as do clock times and date fragments).
_UNIT_NUMBER_RE = re.compile(
    r"(\d{1,3}(?:[   .,]\d{3})+|\d+(?:[.,]\d+)?)\s*"
    r"(%|\$|€|£|dollars?|euros?|bpm|ms|milliards?|millions?|k\$|M\$|btc|bitcoins?)",
    re.IGNORECASE,
)
_BIG_NUMBER_RE = re.compile(r"\b\d{1,3}(?:[   .,]\d{3})+\b|\b\d{4,}\b")
_UNIT_CLASS = {
    "%": "%",
    "$": "$",
    "€": "$",
    "£": "$",
    "dollar": "$",
    "dollars": "$",
    "euro": "$",
    "euros": "$",
    "bpm": "bpm",
    "ms": "ms",
    "milliard": "big",
    "milliards": "big",
    "million": "big",
    "millions": "big",
    "k$": "$",
    "m$": "$",
    "btc": "btc",
    "bitcoin": "btc",
    "bitcoins": "btc",
}


def _digits(raw: str) -> str:
    return re.sub(r"[^\d]", "", raw)


def normalised_key(text: str, limit: int | None = 80) -> str:
    """Lowercase alphanumeric key for near-duplicate detection.

    The one shared normaliser for dedup keys across the briefing modules
    (renderer, composer, synthesis) — keep the semantics here so they cannot
    drift apart again. ``limit=None`` returns the full normalised text (used
    to normalise long blobs rather than keys).
    """
    out = re.sub(r"[^a-z0-9à-ÿ]+", " ", (text or "").lower()).strip()
    return out if limit is None else out[:limit]


def extract_unit_numbers(text: str) -> set[tuple[str, str]]:
    """``(digits, unit-class)`` figures in ``text`` — the inventable kind.

    Dates and years are excluded (they're the date lint's domain, and the
    bullets' own ``(Source, 2026-06-11)`` citations would false-positive on
    every line).
    """
    text = _ISO_RE.sub(" ", text or "")
    out: set[tuple[str, str]] = set()
    for m in _UNIT_NUMBER_RE.finditer(text):
        unit = _UNIT_CLASS.get(m.group(2).lower(), m.group(2).lower())
        out.add((_digits(m.group(1)), unit))
    for m in _BIG_NUMBER_RE.finditer(text):
        digits = _digits(m.group(0))
        if len(digits) == 4 and 1900 <= int(digits) <= 2099:
            continue  # a year, not a quantity
        out.add((digits, "big"))
    return out


def numbers_not_in_source(claim: str, source: str) -> list[str]:
    """Unit-bearing figures in ``claim`` whose digits exist nowhere in
    ``source`` — provable inventions (a figure with the right digits but the
    wrong role still passes; that judgment stays with the critics)."""
    source_digits = {d for d, _ in extract_unit_numbers(source)}
    # Any bare number in the source also counts as support (a bullet may unit
    # a figure the transcript left bare: "75 000" → "75 000 $"). Numbers are
    # tokenised individually — a separator only joins digits when it reads as
    # thousands grouping (groups of exactly 3), so an enumeration like
    # "66, 75" supports BOTH 66 and 75, never a glued "6675".
    source_digits |= {
        _digits(m.group(0))
        for m in re.finditer(r"\d{1,3}(?:[   .,]\d{3})+|\d+(?:[.,]\d+)?", source or "")
    }
    bad: dict[str, str] = {}
    for digits, unit in sorted(extract_unit_numbers(claim or "")):
        if digits and digits not in source_digits and (unit != "big" or digits not in bad):
            bad[digits] = unit
    return [f"{digits} ({unit})" for digits, unit in bad.items()]


def lint_weekdays(draft: str, date_str: str) -> list[dict]:
    """Flag "lundi 16 juin"-style claims whose weekday doesn't match the date.

    The calendar never lies about weekdays; the model does. Conservative: the
    year is unknown in prose, so the claim is flagged only when it is wrong in
    BOTH the briefing year and the next (a January date written in December
    legitimately means next year). The previous year is deliberately NOT
    tolerated: briefing prose almost never dates a recalled past event with a
    weekday, and the extra tolerance would mask the real catches (one in
    three wrong weekdays lands on last year's calendar by chance).
    """
    try:
        year = date.fromisoformat(date_str[:10]).year
    except ValueError:
        return []
    issues: list[dict] = []
    for m in _WEEKDAY_DATE_RE.finditer(draft or ""):
        claimed = _WEEKDAYS[m.group(1).lower()]
        day_num, month = int(m.group(2)), _MONTHS[m.group(3).lower()]
        actual: set[int] = set()
        for y in (year, year + 1):
            try:
                actual.add(date(y, month, day_num).weekday())
            except ValueError:
                continue
        if actual and claimed not in actual:
            issues.append(
                {
                    "type": "weekday_date_mismatch",
                    "excerpt": m.group(0)
                    + " — that date does not fall on that weekday; state the date "
                    "and drop the weekday unless the data names it",
                }
            )
        if len(issues) >= _MAX_DATE_ISSUES:
            break
    return issues


def _iter_text_rows(rows: object) -> Iterator[tuple[str, str, str]]:
    """``(source, when_label, text)`` for every dict in ``rows`` carrying text."""
    if isinstance(rows, dict):
        text = rows.get("text")
        if isinstance(text, str) and text.strip():
            yield (
                str(rows.get("source") or rows.get("label") or ""),
                str(rows.get("when_label") or rows.get("when") or ""),
                text,
            )
        for v in rows.values():
            if isinstance(v, (dict, list, tuple)):
                yield from _iter_text_rows(v)
    elif isinstance(rows, (list, tuple)):
        for v in rows:
            yield from _iter_text_rows(v)


def extract_deadline_lines(rows: object) -> list[str]:
    """Verbatim deadline sentences mined from the data rows.

    A sentence qualifies when it carries both a deadline keyword and an
    explicit date — exactly the material the model must never paraphrase
    loosely. Injected into the vision prompt as untouchable facts.
    """
    out: list[str] = []
    seen: set[str] = set()
    for source, when_label, text in _iter_text_rows(rows):
        for sentence in _SENTENCE_SPLIT_RE.split(text):
            sentence = " ".join(sentence.split())
            if not sentence or len(sentence) < 15:
                continue
            if not _DEADLINE_KEYWORD_RE.search(sentence):
                continue
            if not extract_date_mentions(sentence):
                continue
            key = sentence.lower()[:80]
            if key in seen:
                continue
            seen.add(key)
            suffix = f" [{source} · {when_label}]" if source or when_label else ""
            out.append(sentence[:_MAX_DEADLINE_CHARS] + suffix)
            if len(out) >= _MAX_DEADLINE_LINES:
                return out
    return out
