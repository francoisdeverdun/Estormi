"""Guards against prompt injection in retrieved content."""

import logging
import re

_log = logging.getLogger(__name__)

_INJECTION_PATTERNS = [
    # Allow a couple of filler words between the verb and the anchor so common
    # phrasings ("ignore the previous instructions", "disregard these prior
    # rules", "ignore my earlier instructions") don't slip past — the original
    # `(all\s+)?` only tolerated a literal "all".
    r"(?i)ignore\s+(?:\w+\s+){0,3}(previous|prior|above|earlier|preceding)",
    r"(?i)disregard\s+(?:\w+\s+){0,3}(previous|prior|above|earlier|preceding)",
    r"(?i)you\s+are\s+now\s+a",
    r"(?i)new\s+instructions?\s*:",
    r"(?i)system\s*prompt\s*:",
    r"(?i)\[INST\]",
    r"(?i)<\|system\|>",
    r"(?i)<\|user\|>",
    r"(?i)forget\s+(everything|all)\s+(you|i)",
    r"(?i)act\s+as\s+(if\s+you\s+are|a)",
    r"(?i)DAN\s+mode",
    r"(?i)jailbreak",
]
_COMPILED = [re.compile(p) for p in _INJECTION_PATTERNS]

# Tag-like tokens (``<context>``, ``</threads>``, …) are how prompt assembly
# fences untrusted retrieved content. Retrieved text that embeds a literal
# closing tag could otherwise break out of its fence and pose as trusted
# prompt structure. Neutralise any such token by inserting a zero-width space
# after the ``<`` — visually identical, but no longer a delimiter match.
_TAG_RE = re.compile(r"<(/?[a-zA-Z][a-zA-Z0-9_-]*)>")
_ZWSP = "​"


_REDACTED_MARKER = "[RETRIEVED_CONTENT_REDACTED"


def sanitize_chunk(text: str) -> str:
    """Neutralize potential injection patterns in retrieved text.

    We loop to a fixed point (capped at 5 iterations) so chained patterns
    surface, but skip spans already wrapped in ``_REDACTED_MARKER`` so the
    matched substring inside the replacement can't re-trigger and cause
    runaway nesting.
    """
    for _ in range(5):
        before = text
        # Split on already-redacted spans so we only re-scan untouched text.
        parts = re.split(rf"(\{_REDACTED_MARKER}[^\]]*\])", text)
        for i, part in enumerate(parts):
            if part.startswith(_REDACTED_MARKER):
                continue
            for pattern in _COMPILED:
                part = pattern.sub(
                    # Strip ``]`` from the captured snippet so the marker can't
                    # embed a literal ``]`` that would make the re-scan split
                    # regex (``[^\]]*\]``) terminate early and re-expose the tail.
                    lambda m: (
                        f"{_REDACTED_MARKER}: suspicious pattern '{m.group(0)[:30].replace(']', '')}']"
                    ),
                    part,
                )
            parts[i] = part
        text = "".join(parts)
        if text == before:
            break
    # Defang prompt-fence delimiters last, so a redaction marker can't itself
    # reintroduce one and the injection scan above sees the original text.
    text = _TAG_RE.sub(lambda m: f"<{_ZWSP}{m.group(1)}>", text)
    return text


def sanitize_query(query: str) -> str:
    """Strip null bytes, limit length. Does not remove injection patterns from user queries."""
    query = query.replace("\x00", "").strip()
    if len(query) > 1000:
        _log.debug("sanitize_query truncated input from %d to 1000 chars", len(query))
    return query[:1000]


# Calendar-sync tools (e.g. cross-calendar mirroring) append a machine footer to
# the event description of every copied event. It is pure plumbing — it pollutes
# embeddings at ingestion and leaks verbatim into briefing prose when a draft
# falls back to raw chunk text. Stripped at ingestion (google_calendar/sync) and
# defensively at briefing read time (chunks ingested before the strip existed).
_SYNC_FOOTER_RE = re.compile(
    r"\s*-{2,}\s*Copie synchronisée automatiquement\.?"
    r"(?:\s*Source event ID\s*:\s*\S+)?\s*",
    re.IGNORECASE,
)


def strip_calendar_sync_footer(text: str) -> str:
    """Remove the calendar-sync machine footer anywhere in ``text``."""
    return _SYNC_FOOTER_RE.sub(" ", text or "").strip()
