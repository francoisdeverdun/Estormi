"""Destructive / admin chunk operations: retagging and full DB reset.

These touch both halves of storage (SQLite + Qdrant) and have no place in
the per-source query path. The shared connection state lives on
:mod:`tools` so the test suite's ``tools._db = …`` swap is honoured;
this module reaches the client / settings via ``tools.<name>``.
"""

from __future__ import annotations

import os

import aiosqlite

from estormi_server.sql.schema import (
    INIT_SQL,
    MIGRATION_SQL,
    _apply_chunk_column_migrations,
)


async def retag_chunks(source: str, chat_id_raw: str, group_type: str) -> dict:
    """Retroactively re-apply a group_type tag to every already-ingested chunk
    of one conversation/calendar, in SQLite and the Qdrant payload alike.

    A tag the user sets on a WhatsApp chat or a calendar is normally baked onto
    chunks at ingestion time. This back-fills the chunks already stored so a
    later tag change takes effect on search filtering and the briefing without
    waiting for a re-ingest. Returns {"status": "ok", "retagged": <n>}.
    """
    # Late-binding through ``tools`` so the test suite's
    # ``patch("estormi_server.storage.tools._client", ...)`` and ``tools._db = ...`` swaps are honoured.
    from estormi_server.storage import tools  # noqa: PLC0415

    if not chat_id_raw:
        return {"status": "ok", "retagged": 0}
    db = tools.sqlite_conn()
    cursor = await db.execute(
        "SELECT id FROM chunks WHERE source = ? AND chat_id_raw = ?",
        (source, chat_id_raw),
    )
    ids = [row["id"] for row in await cursor.fetchall()]
    await cursor.close()
    if not ids:
        return {"status": "ok", "retagged": 0}
    # Route the UPDATE → Qdrant set_payload through the canonical ``write_txn``
    # leaf writer: it commits on clean exit and rolls back on ANY abnormal exit —
    # a raised set_payload OR a raised commit. The old hand-rolled lock+commit
    # guarded only set_payload, so a commit-time failure (disk-full, lock
    # contention) left the shared connection wedged in an open transaction — the
    # >1h "database is locked" wedge ``write_txn`` exists to prevent.
    async with tools.write_txn() as db:
        await db.execute(
            "UPDATE chunks SET group_type = ? WHERE source = ? AND chat_id_raw = ?",
            (group_type, source, chat_id_raw),
        )
        await tools._client().set_payload(
            collection_name=tools.COLLECTION,
            payload={"group_type": group_type},
            points=ids,
        )
    return {"status": "ok", "retagged": len(ids)}


async def reset_db() -> None:
    """Close, delete, and reopen estormi.db, recreating the schema from scratch."""
    # Late-binding through ``tools`` so the test suite's ``tools._db = ...``
    # swap is honoured: the global the lifespan owns lives on the tools module.
    from estormi_server.storage import tools  # noqa: PLC0415

    if tools._db is not None:
        try:
            await tools._db.close()
        except Exception:
            pass  # best-effort: closing a stale handle before reopening
        tools._db = None
    for suffix in ("", "-shm", "-wal"):
        try:
            os.unlink(tools.DB_PATH + suffix)
        except OSError:
            pass
    tools._db = await aiosqlite.connect(tools.DB_PATH)
    tools._db.row_factory = aiosqlite.Row
    await tools._db.execute("PRAGMA journal_mode=WAL")
    await tools._db.execute("PRAGMA busy_timeout=30000")
    await tools._db.execute("PRAGMA foreign_keys=ON")
    await tools._db.executescript(INIT_SQL)
    await _apply_chunk_column_migrations(tools._db)
    await tools._db.executescript(MIGRATION_SQL)
    await tools._db.commit()
    # dag_state and engine_lock own their own tables; apply their schemas here
    # too so a reset leaves the DB in the same shape ``server.lifespan`` produces.
    from memory_core.dag_state import ensure_schema as ensure_dag_state_schema  # noqa: PLC0415
    from memory_core.engine_lock import ensure_schema as ensure_engine_lock_schema  # noqa: PLC0415

    await ensure_dag_state_schema(tools._db)
    await ensure_engine_lock_schema(tools._db)
