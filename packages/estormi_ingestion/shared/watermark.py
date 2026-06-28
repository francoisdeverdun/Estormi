"""Read/write ingestion watermarks from estormi.db."""

from __future__ import annotations

from datetime import datetime

import aiosqlite

from .paths import estormi_db_path


def is_future_watermark(last_run: datetime | None, walk_started_at: datetime) -> bool:
    """True if a stored watermark is ahead of the walk start (forward clock-skew).

    The ``documents`` file walker resets ``last_run`` to ``None``
    when this holds, forcing a full rescan rather than permanently skipping files
    whose mtime predates a future-dated watermark. Centralised here so the guard
    cannot drift between the two walkers, and so tests exercise the real
    predicate instead of a hand-copied replica.
    """
    return last_run is not None and last_run > walk_started_at


# Resolve the DB path per call (not at import) so an in-process ESTORMI_DB /
# ESTORMI_DATA_DIR override applied after import still routes watermarks to the
# same DB the rest of the process writes chunks to. Matches the use-time
# resolution in whatsapp/ingest_conversations.py and google_calendar/sync.py.


async def get_watermark(source: str) -> tuple[str | None, str | None]:
    """Return (last_fetched_at, last_item_id) or (None, None) on first run."""
    async with aiosqlite.connect(estormi_db_path(), timeout=30) as db:
        # WAL is persistent in the DB file, but set it defensively for a fresh
        # DB where this may be the first writer; the higher busy_timeout makes a
        # watermark write wait out a long ingest write instead of erroring with
        # "database is locked".
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute("PRAGMA busy_timeout=30000")
        cursor = await db.execute(
            "SELECT last_fetched_at, last_item_id FROM ingestion_watermarks WHERE source = ?",
            (source,),
        )
        row = await cursor.fetchone()
        return (row[0], row[1]) if row else (None, None)


async def set_watermark(source: str, fetched_at: str, item_id: str | None = None) -> None:
    """Persist watermark — call only on success (exit code 0)."""
    async with aiosqlite.connect(estormi_db_path(), timeout=30) as db:
        # WAL is persistent in the DB file, but set it defensively for a fresh
        # DB where this may be the first writer; the higher busy_timeout makes a
        # watermark write wait out a long ingest write instead of erroring with
        # "database is locked".
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute("PRAGMA busy_timeout=30000")
        await db.execute(
            """INSERT INTO ingestion_watermarks (source, last_fetched_at, last_item_id)
               VALUES (?, ?, ?)
               ON CONFLICT(source) DO UPDATE SET
                   last_fetched_at = excluded.last_fetched_at,
                   last_item_id    = excluded.last_item_id""",
            (source, fetched_at, item_id),
        )
        await db.commit()
