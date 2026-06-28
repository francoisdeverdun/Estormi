"""Phase ② — turn the harvested briefings into stage-shaped training pairs.

Each harvested briefing yields pairs whose PROMPT is rebuilt from that day's
own facts (calendar / reminders / WHOOP read back from the chunk store) and
whose TARGET is the briefing's own text for the matching stage — the same
instruction skeletons the composition stages use, so the adapter learns the
gesture in the shape it will be asked for.

Validation holds out whole DAYS, never random pairs: the eval must measure
generalisation to unseen days, not recall of seen ones.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from datetime import datetime
from pathlib import Path

from estormi_briefing.compose.exemplars import harvest_exemplars
from estormi_distill.paths import dataset_dir, refs_dir
from estormi_ingestion.shared.config import mcp_url
from estormi_ingestion.shared.paths import estormi_db_path
from memory_core.timeparse import resolve_local_tz

log = logging.getLogger("distill")

# Local timezone, resolved the same way the briefing engine does
# (memory_core.timeparse, honoring ESTORMI_LOCAL_TZ) so the %H:%M times baked
# into the training prompts match the machine-local times in the harvested
# briefing prose — no region is hardcoded.
_TZ = resolve_local_tz()
# Held-out share of days for validation (whole days, spread over the range).
_VALID_DAYS_MIN, _VALID_DAYS_MAX = 1, 3

LEDE_INSTR = (
    "Tu écris la phrase d'ouverture (lede) d'un briefing quotidien personnel, en "
    "français, tutoiement. Une seule phrase, sobre et concrète : NOMME l'événement-"
    "pivot (une réunion, une personne, un lieu précis) et ce vers quoi la journée "
    "converge — jamais une simple amplitude horaire (« une journée de 9h à 18h »), "
    "jamais de jargon ni de métaphore.\nAGENDA DU JOUR : {tl}"
)
READINESS_INSTR = (
    "Tu écris la ligne « Forme du jour » d'un briefing personnel, en français, "
    "tutoiement. Une à deux phrases : un vrai conseil croisant la forme physique et "
    "l'agenda, au plus deux chiffres de santé (les heures ne comptent pas), et ne "
    "déclare jamais une séance « prévue » si l'agenda n'en porte pas.\n"
    "SANTÉ : {whoop}\nAGENDA : {tl}"
)
WRITER_INSTR = (
    "Tu écris un paragraphe du briefing « Ma journée », en français, tutoiement. "
    "2 à 3 phrases reliées : l'enjeu ou la conséquence concrète qui relie les "
    "faits (préparation, décision à prendre), jamais leur simple enchaînement "
    "horaire.\nFAITS : {tl}\nRAPPELS : {rem}"
)
IMPACT_INSTR = (
    "Tu écris la ligne « → Impact » d'un item du monde dans un briefing personnel : "
    "une phrase, tutoiement, la conséquence directe et concrète pour l'utilisateur "
    "(profil ci-dessous), enracinée dans le vocabulaire du profil.\n"
    "PROFIL : {profile}\nITEM : {item}"
)

_IMPACT_ITEM_RE = re.compile(r"<li>(.*?)→\s*Impact\s*:\s*([^<\[]+)", re.DOTALL)
_TAG_RE = re.compile(r"<[^>]+>")

# Hand-corrected briefings are the highest-quality targets there are, but a
# user edits only a handful of days. Repeating their pairs in the TRAIN split
# gives those gradients more weight than their raw day-count, so the quill
# leans toward the corrections instead of being drowned by the machine archive.
# Validation is never up-weighted (it must stay an honest held-out count).
_EDITED_REPEAT = 3


def stage_of_prompt(prompt: str) -> str:
    """Which briefing stage a training/eval prompt belongs to.

    Anchored to the stable *prefix* of each ``*_INSTR`` skeleton above (every
    skeleton opens ``Tu écris …`` with a distinct continuation), not a loose
    substring: the interpolated PROFIL/ITEM text of an Impact prompt can itself
    contain « Forme du jour » or « Ma journée », which a substring scan would
    misclassify. ``other`` for anything that matches none (never expected).
    """
    if prompt.startswith("Tu écris la phrase d'ouverture"):
        return "lede"
    if prompt.startswith("Tu écris la ligne « Forme du jour »"):
        return "readiness"
    if prompt.startswith("Tu écris un paragraphe du briefing « Ma journée »"):
        return "writer"
    if prompt.startswith("Tu écris la ligne « → Impact »"):
        return "impact"
    return "other"


# Sentinels for a day with no facts of that kind — shared by ``day_facts`` (the
# prompt text) and the harvest guard (whether the day is grounded at all).
_NO_TIMELINE = "(agenda vide)"
_NO_REMINDERS = "(aucun)"
_NO_WHOOP = "(aucune donnée)"


def _hhmm(iso: str) -> str:
    try:
        return datetime.fromisoformat(iso).astimezone(_TZ).strftime("%H:%M")
    except (ValueError, TypeError):
        return ""


def _whoop_text(day: str) -> str:
    """The day's WHOOP snapshot text — via the server's ``/fetch_around``.

    SQLite carries chunk METADATA only; the text lives in Qdrant, served by
    the MCP retrieval endpoint. Best-effort: prompts degrade to "(aucune
    donnée)" rather than blocking the dataset on a health-less day.
    """
    import httpx  # noqa: PLC0415

    base = mcp_url()
    try:
        resp = httpx.post(
            f"{base}/fetch_around",
            json={"date": day, "window_days": 1, "corpus": "personal", "sources": ["whoop"]},
            timeout=15.0,
        )
        chunks = resp.json().get("chunks") or []
        same_day = [c for c in chunks if str(c.get("date_ts") or "").startswith(day)]
        text = str((same_day or chunks or [{}])[0].get("text") or "")
        return " ".join(text.split())[:260]
    except Exception:  # noqa: BLE001 — degrade, never block
        return ""


def day_facts(conn: sqlite3.Connection, day: str) -> tuple[str, str, str]:
    """``(timeline, reminders, whoop)`` for ``day``.

    Timeline + reminders come from the SQLite chunk metadata (titles and
    timestamps are columns); the WHOOP text comes from retrieval.
    """
    rows = conn.execute(
        "SELECT title, date_ts, end_date_ts FROM chunks "
        "WHERE source IN ('gcal','calendar') AND substr(date_ts,1,10)=? ORDER BY date_ts",
        (day,),
    ).fetchall()
    timeline: list[str] = []
    for title, start, end in rows:
        s, e = _hhmm(start or ""), _hhmm(end or "")
        if s and e and s != e:
            timeline.append(f"{s}–{e} {title}")
        elif s:
            timeline.append(f"{s} {title}")
        else:
            timeline.append(f"(journée) {title}")
    reminders = [
        r[0]
        for r in conn.execute(
            "SELECT title FROM chunks WHERE source='reminders' AND substr(date_ts,1,10)=?",
            (day,),
        ).fetchall()
    ]
    return (
        "; ".join(timeline) or _NO_TIMELINE,
        "; ".join(reminders) or _NO_REMINDERS,
        _whoop_text(day) or _NO_WHOOP,
    )


def pairs_for_reference(
    html_body: str,
    timeline: str,
    reminders: str,
    whoop: str,
    profile: str,
    *,
    facts_present: bool = True,
) -> list[tuple[str, str]]:
    """Stage-shaped ``(prompt, target)`` pairs from one harvested briefing.

    When ``facts_present`` is false the day has no chunks left (an old archived
    briefing whose source data was pruned): its lede/readiness/writer prompts
    would degrade to empty sentinels, teaching the quill to write prose
    ungrounded in facts, so only the impact pairs (item + profile come from the
    HTML, not the day's facts) are emitted.
    """
    harvested = harvest_exemplars(html_body)
    pairs: list[tuple[str, str]] = []
    if facts_present:
        for lede in harvested["lede"]:
            pairs.append((LEDE_INSTR.format(tl=timeline), lede))
        for line in harvested["readiness"]:
            pairs.append((READINESS_INSTR.format(whoop=whoop, tl=timeline), line))
        for paragraph in harvested["writer"]:
            pairs.append((WRITER_INSTR.format(tl=timeline, rem=reminders), paragraph))
    for m in _IMPACT_ITEM_RE.finditer(html_body):
        item = " ".join(_TAG_RE.sub(" ", m.group(1)).split())[:300]
        clause = " ".join(m.group(2).split()).strip(" .")
        if item and clause:
            pairs.append((IMPACT_INSTR.format(profile=profile, item=item), f"→ Impact : {clause}."))
    return pairs


def held_out_days(days: list[str]) -> set[str]:
    """Deterministic whole-day validation split, spread across the range."""
    days = sorted(days)
    if len(days) < 4:
        return set(days[-1:])  # tiny stock: one day still keeps eval honest
    k = max(_VALID_DAYS_MIN, min(_VALID_DAYS_MAX, len(days) // 6))
    step = len(days) // (k + 1)
    return {days[step * (i + 1)] for i in range(k)}


def build_dataset(db_path: Path | None = None) -> dict:
    """Harvest every workspace reference into ``dataset/{train,valid}.jsonl``.

    Returns counters for the status file: pair counts, day counts, the
    held-out days and the reference-model mix.
    """
    refs = sorted(refs_dir().glob("????-??-??.json"))
    conn = sqlite3.connect(db_path or estormi_db_path())
    row = conn.execute("SELECT value FROM settings WHERE key='briefing_user_context'").fetchone()
    profile = (row[0] if row else "")[:800]

    days = [p.stem for p in refs]
    valid_days = held_out_days(days)
    train, valid = [], []
    models: dict[str, int] = {}
    for path in refs:
        day = path.stem
        try:
            payload = json.loads(path.read_text())
        except Exception:  # noqa: BLE001 — unreadable ref contributes nothing
            log.warning("dataset: unreadable reference %s — skipped", path.name)
            continue
        model = str(payload.get("referenceModel") or "?")
        models[model] = models.get(model, 0) + 1
        timeline, reminders, whoop = day_facts(conn, day)
        facts_present = not (
            timeline == _NO_TIMELINE and reminders == _NO_REMINDERS and whoop == _NO_WHOOP
        )
        samples = [
            {
                "messages": [
                    {"role": "user", "content": prompt},
                    {"role": "assistant", "content": target},
                ]
            }
            for prompt, target in pairs_for_reference(
                payload.get("htmlBody") or "",
                timeline,
                reminders,
                whoop,
                profile,
                facts_present=facts_present,
            )
        ]
        if day in valid_days:
            valid.extend(samples)
        else:
            # Up-weight hand-corrected days; the machine archive stays ×1.
            reps = _EDITED_REPEAT if model == "user-edited" else 1
            for _ in range(reps):
                train.extend(samples)
    conn.close()

    out = dataset_dir()
    out.mkdir(parents=True, exist_ok=True)
    for name, rows in (("train", train), ("valid", valid)):
        with open(out / f"{name}.jsonl", "w") as f:
            for sample in rows:
                f.write(json.dumps(sample, ensure_ascii=False) + "\n")
    counters = {
        "train": len(train),
        "valid": len(valid),
        "days": len(days),
        "validDays": sorted(valid_days),
        "models": models,
        "editedRepeat": _EDITED_REPEAT,
    }
    log.info("dataset: %s", counters)
    return counters
