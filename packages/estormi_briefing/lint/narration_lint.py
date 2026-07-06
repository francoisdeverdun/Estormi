"""Deterministic regression check for the spoken-edition narration.

The audio path (``io/delivery.py``) asks the LLM to re-voice the whole briefing
as flowing narration before synthesis. That rewrite is meant to keep every fact
while stripping visual scaffolding — but a small/flaky model sometimes returns a
stub: a single greeting, a truncated first paragraph, an empty shell. Shipping
that as the day's audio is worse than reading the body verbatim (which is the
already-trusted fallback).

:func:`narration_regressed` is a cheap, never-raising guard that flags the
gross-loss cases the rewrite must not produce: a narration far shorter than the
body, or one that has dropped the title line, every clock time, or every source
token. It is deliberately conservative — the rewrite legitimately shrinks the
text (bullets and markup vanish) and respells times/percentages for the ear —
so the thresholds are loose and every clause errs toward *not* flagging. On a
flag the caller falls back to the verbatim body, never to no audio.
"""

from __future__ import annotations

import re

# The spoken rewrite drops scaffolding (bullets, headings, citations) and merges
# sentences, so it is always shorter than the body. This floor only trips on a
# gross loss — a stub that kept a small fraction of the content.
_MIN_WORD_RATIO = 0.45

_WORD_RE = re.compile(r"[^\W\d_]+", re.UNICODE)

# Clock forms in the on-screen body: "13 h", "13h30", "09:15", "9 h 30". The
# narration respells these for the ear ("treize heures"), so we do NOT require a
# digit clock to survive — see :func:`_has_time`, which also accepts the spoken
# hour word.
_CLOCK_RE = re.compile(r"\b\d{1,2}\s*[:h]\s*\d{0,2}\b")
# Spoken time in the narration: "treize heures", "9 heures", "midi", "minuit".
_SPOKEN_TIME_RE = re.compile(r"\b(heures?|midi|minuit|o'?clock)\b", re.IGNORECASE)

# Source provenance the body carries: an inline "— <source> · <date>" tail, a
# "[SOURCE: … | …]" marker, or a bare interpunct-separated attribution. The
# narration weaves these in as words ("d'après <source>"), so — like times — we
# accept a woven form via :func:`_has_source`.
_SOURCE_MARKER_RE = re.compile(r"\[source:|·|—\s*\w", re.IGNORECASE)
_WOVEN_SOURCE_RE = re.compile(
    r"\b(d'après|selon|comme (?:le|l')|rapporte|explique|according to|reports?)\b",
    re.IGNORECASE,
)


def _word_count(text: str) -> int:
    """Count spoken words (letters only) — digits/punctuation don't count."""
    return len(_WORD_RE.findall(text or ""))


def _title_tokens(title: str) -> list[str]:
    """Distinctive lower-cased word tokens of the title (length ≥ 3)."""
    return [w.lower() for w in _WORD_RE.findall(title or "") if len(w) >= 3]


def _has_time(text: str) -> bool:
    """A clock time is present as a digit form OR spelled for the ear."""
    return bool(_CLOCK_RE.search(text) or _SPOKEN_TIME_RE.search(text))


def _has_source(text: str) -> bool:
    """A source is present as a raw marker OR woven into a sentence."""
    return bool(_SOURCE_MARKER_RE.search(text) or _WOVEN_SOURCE_RE.search(text))


def narration_regressed(body_text: str, narration: str, title: str = "") -> bool:
    """True when the spoken rewrite has clearly lost the briefing's content.

    Conservative by design: returns ``True`` only for the gross-loss cases where
    reading the verbatim body would be the safer audio. Never raises.

    Flags when, relative to the body, the narration:
      * keeps fewer than ``_MIN_WORD_RATIO`` of its spoken words, OR
      * drops the title line (none of the title's distinctive tokens survive), OR
      * drops every clock time the body carried, OR
      * drops every source the body carried.
    """
    body_text = body_text or ""
    narration = narration or ""

    # An empty narration is a regression only if there was something to say.
    if not narration.strip():
        return bool(body_text.strip())

    body_words = _word_count(body_text)
    narr_words = _word_count(narration)
    if body_words and narr_words < _MIN_WORD_RATIO * body_words:
        return True

    tokens = _title_tokens(title)
    if tokens:
        low = narration.lower()
        if not any(tok in low for tok in tokens):
            return True

    if _has_time(body_text) and not _has_time(narration):
        return True

    if _has_source(body_text) and not _has_source(narration):
        return True

    return False
