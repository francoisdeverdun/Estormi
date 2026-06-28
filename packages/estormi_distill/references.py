"""Phase ① — mirror the user's briefing archive into the refs workspace.

The training corpus is the user's OWN briefings, not synthetic cloud
compositions: every ``briefings/<day>.json`` in the vault (composed locally
and, increasingly, corrected by hand — on macOS or by editing the iCloud-Drive
file directly) is copied into ``refs/<day>.json`` so ``dataset.build_dataset``
can harvest it. External edits need no hook: they land in the very vault JSON
this module reads.
"""

from __future__ import annotations

import json
import logging
import os

from estormi_distill.paths import refs_dir

log = logging.getLogger("distill")

# Minimum number of vault briefings before distillation is worth running. Below
# this the dataset is too thin to teach the quill anything, so the engine
# refuses and the Officina card greys the button out. Single source of truth for
# both the run-time gate (``run_distill``) and the UI gate (the status endpoint
# surfaces it to ``DistillationCard``).
MIN_BRIEFINGS = 5


def existing_references() -> dict[str, dict]:
    """``{date: meta}`` for every reference already in the workspace."""
    out: dict[str, dict] = {}
    for path in sorted(refs_dir().glob("????-??-??.json")):
        try:
            payload = json.loads(path.read_text())
            out[path.stem] = {"model": str(payload.get("referenceModel") or "")}
        except Exception:  # noqa: BLE001 — unreadable ref = re-harvest it
            continue
    return out


def _write_reference(day: str, html_body: str, model: str) -> dict:
    """Atomically write ``refs/<day>.json`` (temp + rename): a concurrent
    ``dataset.build_dataset`` harvest must never read a half-written file."""
    safe = day.replace("/", "_").replace("\\", "_").strip() or day
    refs_dir().mkdir(parents=True, exist_ok=True)
    payload = {"date": day, "htmlBody": html_body, "referenceModel": model}
    dest = refs_dir() / f"{safe}.json"
    tmp = dest.with_name(f"{dest.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    tmp.replace(dest)
    return payload


def harvest_archive() -> int:
    """Mirror every vault briefing into the refs workspace; return the count.

    Stamps ``referenceModel`` so the mixed-stock readout can surface the
    human-curated share — ``user-edited`` when the briefing carries an
    ``editedAt``, else ``archive``. The refs workspace is kept as an exact
    mirror of the vault: references whose briefing was deleted are dropped so
    the next dataset never trains on a stale day.
    """
    from estormi_ingestion.shared.delivery.vault_sync import (  # noqa: PLC0415
        list_briefings,
        read_briefing,
    )

    refs = refs_dir()
    refs.mkdir(parents=True, exist_ok=True)
    written: set[str] = set()
    for meta in list_briefings():
        day = str(meta.get("date") or "").strip()
        if not day:
            continue
        briefing = read_briefing(day)
        html_body = (briefing or {}).get("htmlBody")
        if not briefing or not html_body:
            continue
        model = "user-edited" if briefing.get("editedAt") else "archive"
        _write_reference(day, html_body, model)
        written.add(f"{day}.json")
    for path in refs.glob("????-??-??.json"):
        if path.name not in written:
            path.unlink(missing_ok=True)
    log.info("archive harvest: %d briefing(s) mirrored into refs", len(written))
    return len(written)


def vault_briefing_count() -> int:
    """How many briefings the vault holds — trainable even before a harvest.

    Lets the card show the real "ready to train on" count instead of the empty
    refs workspace on a machine that has never distilled.
    """
    from estormi_ingestion.shared.delivery.vault_sync import list_briefings  # noqa: PLC0415

    try:
        return sum(1 for m in list_briefings() if str(m.get("date") or "").strip())
    except Exception:  # noqa: BLE001 — vault unreadable: fall back to "unknown"
        return 0


def register_edited_reference(day: str, html_body: str) -> dict:
    """Register a user-edited briefing as a distillation reference immediately.

    A human-corrected briefing is the highest-quality training target there is,
    so an edit made on macOS folds straight into the quill's reference set
    (``dataset.build_dataset`` harvests every kept ref on the next retrain). The
    next ``harvest_archive`` would pick the same edit up from the vault, but
    registering it on save makes it available without waiting for a harvest.
    Stamped ``referenceModel="user-edited"``; overwrites any prior ref for the day.
    """
    payload = _write_reference(day, html_body, "user-edited")
    log.info("reference %s registered from a user edit", day)
    return payload
