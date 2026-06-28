"""Read-only macOS-permission gate for connector runs.

The permission *preflight* (server-side, run with the app in the foreground so
the prompt is attributed to Estormi) is the **only** place that probes or
requests a macOS TCC permission. This module never touches TCC: it reads the
status the preflight persisted to the ``settings`` table and lets a connector
decide whether to run or skip. That keeps the pipeline run *request-free* — no
permission dialog can surface mid-pipeline, where (under the scheduler) nobody is
around to answer it.

``connectors run <stage>`` consults :func:`is_blocked_status` before invoking a
connector and exits :data:`SKIP_EXIT_CODE` when blocked; ``daily_ingestion.sh``
maps that code to a first-class ``skipped`` stage status rather than a failure.
"""

from __future__ import annotations

import json
import sqlite3

# EX_TEMPFAIL. A connector run skipped for a missing permission exits with this
# code; ``scripts/daily_ingestion.sh`` recognises it and records the stage as
# ``skipped`` (not ``failed``), so the UI shows "needs permission" rather than
# an error.
SKIP_EXIT_CODE = 75


def persisted_permission_status(source: str) -> str | None:
    """Return the last TCC status the preflight recorded for ``source``.

    One of ``authorized`` / ``denied`` / ``undetermined`` / ``manual`` /
    ``unavailable``, or ``None`` when no status was ever recorded (a freshly
    enabled source the preflight hasn't visited yet). Reads the live DB
    read-only; never raises — any lookup failure degrades to ``None`` so the
    gate fails *open* rather than wedging ingestion.
    """
    try:
        from memory_core.dag_state import db_path  # noqa: PLC0415

        path = db_path()
    except Exception:
        return None
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5.0)
    except sqlite3.Error:
        return None
    try:
        cur = conn.execute(
            "SELECT value FROM settings WHERE key = ?", (f"source_{source}_permission",)
        )
        row = cur.fetchone()
    except sqlite3.Error:
        return None
    finally:
        conn.close()
    if not row or not row[0]:
        return None
    try:
        return json.loads(row[0]).get("status")
    except (ValueError, TypeError):
        return None


def is_blocked_status(status: str | None) -> bool:
    """Whether a recorded status should block the run.

    ``None`` (never probed) and ``authorized`` run; anything else blocks. We
    deliberately let ``None`` through: the preflight populates every enabled
    source's status at app launch, so by the time the scheduled pipeline fires the
    status is known. Blocking on ``None`` would only punish the transient
    window right after enabling — and that path already probed at toggle time.
    """
    return status not in (None, "authorized")
