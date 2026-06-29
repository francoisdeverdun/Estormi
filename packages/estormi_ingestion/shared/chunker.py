"""Text chunkers — a plain sliding window and a structure-aware variant."""

from __future__ import annotations

import re


def sliding_chunks(text: str, size: int = 800, overlap: int = 100) -> list[str]:
    """Split text into overlapping chunks of ~size characters."""
    # An overlap >= size collapses the sliding step to 0 (clamped to 1
    # below), which would emit nearly-N copies of the same content
    # instead of a sliding window. Catch that misconfiguration at the
    # boundary rather than at retrieval time.
    if overlap >= size:
        raise ValueError(f"overlap ({overlap}) must be strictly smaller than size ({size})")
    text = (text or "").strip()
    if not text:
        return []
    if len(text) <= size:
        return [text]
    step = max(1, size - overlap)
    chunks: list[str] = []
    for i in range(0, len(text), step):
        chunk = text[i : i + size].strip()
        if chunk:
            chunks.append(chunk)
        # Once a chunk reaches the end of the text, the window is fully
        # covered. Continuing would emit a near-empty final chunk that only
        # duplicates the tail already present in this one's overlap region.
        if i + size >= len(text):
            break
    return chunks


# Sentence boundary: ., ! or ? followed by whitespace. Deliberately naive —
# it over-splits on abbreviations, which is harmless here (chunks stay a
# little smaller) and far cheaper than a real sentence tokenizer.
_SENTENCE_RE = re.compile(r"(?<=[.!?])\s+")
# A blank line (optionally carrying stray whitespace) marks a paragraph break.
_PARAGRAPH_RE = re.compile(r"\n\s*\n")


def _emit(out: list[str], chunk: str, min_size: int) -> None:
    chunk = chunk.strip()
    if len(chunk) >= min_size:
        out.append(chunk)


def paragraph_chunks(text: str, max_size: int = 800, min_size: int = 80) -> list[str]:
    """Structure-aware chunker for prose notes and documents.

    Unlike :func:`sliding_chunks`, which slices on a fixed character window
    and routinely splits mid-thought, this respects the text's own logical
    boundaries so a single chunk does not straddle two unrelated subjects
    (the root cause of cross-source fusion in the briefing):

    1. Split on blank lines into paragraphs.
    2. A paragraph over ``max_size`` is re-split on sentence boundaries,
       packing consecutive sentences back up to ``max_size``.
    3. A single sentence still over ``max_size`` falls back to
       :func:`sliding_chunks`.

    Fragments shorter than ``min_size`` are dropped as too small to carry
    retrievable meaning — except when that would discard the whole text, in
    which case the stripped text is returned intact so a short note is never
    silently lost.
    """
    text = (text or "").strip()
    if not text:
        return []

    out: list[str] = []
    for para in _PARAGRAPH_RE.split(text):
        para = para.strip()
        if not para:
            continue
        if len(para) <= max_size:
            _emit(out, para, min_size)
            continue

        # Paragraph too long — repack by sentence.
        buf = ""
        for sent in _SENTENCE_RE.split(para):
            sent = sent.strip()
            if not sent:
                continue
            if len(sent) > max_size:
                # One oversized sentence: flush what we have, then window it.
                if buf:
                    _emit(out, buf, min_size)
                    buf = ""
                overlap = min(max_size // 8, max_size - 1)
                for sub in sliding_chunks(sent, size=max_size, overlap=overlap):
                    # Append unconditionally: these sub-windows are slices of one
                    # real sentence, so a trailing window under min_size must not
                    # be dropped by _emit — the whole-text rescue below only fires
                    # when ``out`` is entirely empty and wouldn't catch this.
                    out.append(sub)
                continue
            if buf and len(buf) + 1 + len(sent) > max_size:
                _emit(out, buf, min_size)
                buf = sent
            else:
                buf = f"{buf} {sent}".strip()
        if buf:
            _emit(out, buf, min_size)

    # Never drop a whole note just because every fragment fell under min_size
    # (e.g. a terse one-line note). Keep the text as a single chunk.
    if not out:
        return [text]
    return out
