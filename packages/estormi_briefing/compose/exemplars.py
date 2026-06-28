"""Few-shot exemplar bank — cloud-quality output as style anchors.

The bench's cloud briefings (Fable/Opus) carry exactly what the local models
under-deliver: preparation advice in the prose, grounded impact lines, a lede
that names the day's shape. Those outputs are PERSONAL data, so the bank
lives in the data dir (``briefing_exemplars.json``) and is harvested locally
by ``stage_harness harvest`` — it must never enter the repo.

At composition time, :func:`exemplar_block` returns a bounded "EXAMPLES"
prompt block for a stage ("lede", "readiness", "impact", "writer"); stages
inject it when the bank exists and stay byte-identical without it. In-context
style transfer: 100 % local at runtime, the cloud only ever acted as a
dev-time reference.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import structlog

from memory_core.settings import resolve_data_dir

log = structlog.get_logger()

EXEMPLARS_FILE = "briefing_exemplars.json"
# Per-stage caps: how many exemplars one prompt may carry, and the total
# character budget — a style anchor, not a context-window tax.
_MAX_PER_PROMPT = 2
_MAX_BLOCK_CHARS = 700
# How many exemplars the bank retains per stage (newest first).
_MAX_PER_STAGE = 6

_KNOWN_STAGES = ("lede", "readiness", "impact", "writer")


def _bank_path() -> Path:
    # Resolved per call (not at import) so the test suite's ESTORMI_DATA_DIR
    # override and the bundle's env always win.
    return Path(resolve_data_dir()) / EXEMPLARS_FILE


def load_bank() -> dict[str, list[dict]]:
    """The bank's ``{stage: [{"text", "from"}]}`` map — ``{}`` when absent."""
    try:
        data = json.loads(_bank_path().read_text())
        if not isinstance(data, dict):
            return {}
    except FileNotFoundError:
        return {}
    except Exception:  # noqa: BLE001 — a corrupt bank must never block a run
        log.warning("exemplars: bank unreadable — ignored")
        return {}
    out: dict[str, list[dict]] = {}
    for stage, items in data.items():
        if stage not in _KNOWN_STAGES or not isinstance(items, list):
            continue
        clean = [
            {"text": str(i.get("text") or "").strip(), "from": str(i.get("from") or "")}
            for i in items
            if isinstance(i, dict) and str(i.get("text") or "").strip()
        ]
        if clean:
            out[stage] = clean[:_MAX_PER_STAGE]
    return out


def save_bank(bank: dict[str, list[dict]]) -> Path:
    path = _bank_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(bank, ensure_ascii=False, indent=1))
    return path


def add_exemplars(stage: str, texts: list[str], source: str) -> int:
    """Merge ``texts`` into the bank under ``stage`` (newest first, deduped).

    Returns how many genuinely new exemplars were added.
    """
    if stage not in _KNOWN_STAGES:
        raise ValueError(f"unknown exemplar stage {stage!r} (known: {_KNOWN_STAGES})")
    bank = load_bank()
    existing = bank.get(stage, [])
    seen = {" ".join(i["text"].lower().split())[:80] for i in existing}
    fresh = []
    for t in texts:
        t = " ".join((t or "").split())
        key = t.lower()[:80]
        if not t or key in seen:
            continue
        seen.add(key)
        fresh.append({"text": t, "from": source})
    if fresh:
        bank[stage] = (fresh + existing)[:_MAX_PER_STAGE]
        save_bank(bank)
    return len(fresh)


def exemplar_block(stage: str, language: str = "French") -> str:
    """A bounded EXAMPLES block for ``stage``'s prompt — ``""`` when no bank.

    The header makes the contract explicit: imitate the STYLE (density,
    concreteness, the way facts are linked), never the content — the
    exemplars describe a different day.
    """
    items = load_bank().get(stage) or []
    if not items:
        return ""
    lines: list[str] = []
    used = 0
    for item in items[:_MAX_PER_PROMPT]:
        text = item["text"]
        if used + len(text) > _MAX_BLOCK_CHARS:
            break
        used += len(text)
        lines.append(f"- {text}")
    if not lines:
        return ""
    return (
        "EXAMPLES (imitate the STYLE — density, concreteness, how facts are "
        "linked. They describe a DIFFERENT day: never reuse their facts, "
        "names or numbers):\n" + "\n".join(lines)
    )


# ── Harvesting exemplars from a composed briefing ────────────────────────────
# The lede is the line after the title, the readiness card follows "✦ Forme du
# jour", impact clauses ride inside world bullets, and the writer prose is the
# "Ma journée" paragraphs inside the <!--myday:start-->…<!--myday:end--> markers
# (legacy briefings without markers fall back to the timeline→reminders span).
# Driven by the `stage_harness harvest` CLI, but kept here next to the bank it
# feeds (production code, not a dev-only harness helper).
_TAG_RE = re.compile(r"<[^>]+>")
_IMPACT_RE = re.compile(r"→\s*Impact\s*:?\s*([^<\n\[]+)")
_TIMELINE_ROW_RE = re.compile(r"^\d{2}:\d{2}–\d{2}:\d{2}\b")
_MYDAY_RE = re.compile(r"<!--myday:start-->(.*?)<!--myday:end-->", re.DOTALL)
_PARA_RE = re.compile(r"<p\b[^>]*>(.*?)</p>", re.DOTALL)
_SOURCE_SPAN_RE = re.compile(r'<span class="source".*?</span>', re.DOTALL)


def _briefing_text_lines(html_body: str) -> list[str]:
    import html as _html

    text = _TAG_RE.sub("\n", html_body)
    text = _html.unescape(text)
    return [ln.strip() for ln in text.splitlines() if ln.strip()]


def _clean_para(para_html: str) -> str:
    """One ``<p>`` of "Ma journée" prose → plain text, minus its source span."""
    import html as _html

    para_html = _SOURCE_SPAN_RE.sub("", para_html)
    return " ".join(_html.unescape(_TAG_RE.sub(" ", para_html)).split())


def harvest_exemplars(html_body: str) -> dict[str, list[str]]:
    """Extract per-stage style exemplars from one composed briefing's HTML."""
    lines = _briefing_text_lines(html_body)
    out: dict[str, list[str]] = {"lede": [], "readiness": [], "impact": [], "writer": []}
    if len(lines) >= 2 and not lines[1].startswith(("✦", "📅", "🔭", "🌍", "📺")):
        out["lede"].append(lines[1])
    for i, ln in enumerate(lines):
        if ln.startswith("✦") and i + 1 < len(lines):
            nxt = lines[i + 1]
            if len(nxt) > 60:
                out["readiness"].append(nxt)
    out["impact"] = [
        " ".join(m.group(1).split()).strip(" .") for m in _IMPACT_RE.finditer(html_body)
    ]
    # Writer prose ("Ma journée"): prefer the explicit myday markers — each <p>
    # inside is one writer paragraph, minus its source attribution. Fall back to
    # the timeline→reminders heuristic for legacy briefings without markers.
    myday = _MYDAY_RE.search(html_body)
    if myday:
        writer = []
        for para in _PARA_RE.findall(myday.group(1)):
            text = _clean_para(para)
            if len(text) > 80:
                writer.append(text)
        out["writer"] = writer
        return out
    try:
        last_row = max(i for i, ln in enumerate(lines) if _TIMELINE_ROW_RE.match(ln))
        stop = next(i for i, ln in enumerate(lines) if ln.startswith("À ne pas oublier"))
    except (ValueError, StopIteration):
        return out
    out["writer"] = [
        ln for ln in lines[last_row + 1 : stop] if len(ln) > 100 and not ln.startswith("—")
    ]
    return out
