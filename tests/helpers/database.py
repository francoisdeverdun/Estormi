"""SQLite helpers shared by unit, integration, and E2E tests."""

from __future__ import annotations

import sqlite3

import aiosqlite


async def apply_runtime_schema(conn: aiosqlite.Connection) -> None:
    """Apply the production SQLite schema to a test connection.

    Mirrors production startup exactly — the same three ordered steps as
    ``server.lifespan`` / ``chunk_admin.reset_db``: ``INIT_SQL`` →
    ``_apply_chunk_column_migrations`` (the additive ALTER pass) →
    ``MIGRATION_SQL``. The middle pass MUST run before ``MIGRATION_SQL`` so the
    latter's indexes/backfills can reference the additive columns (e.g.
    ``end_date_ts``); omitting it diverges from the real startup path and an
    upgrade-from-old-DB test would fail where production succeeds. Each block is
    run via ``executescript`` (not split on ``;``) because MIGRATION_SQL carries
    SQL comments containing ``;`` that a naive splitter would corrupt.
    """
    from estormi_server.sql.schema import (
        INIT_SQL,
        MIGRATION_SQL,
        _apply_chunk_column_migrations,
    )
    from memory_core.dag_state import ensure_schema as ensure_dag_state_schema
    from memory_core.engine_lock import ensure_schema as ensure_engine_lock_schema

    await conn.execute("PRAGMA journal_mode=WAL")
    await conn.execute("PRAGMA busy_timeout=10000")
    await conn.execute("PRAGMA foreign_keys=ON")
    await conn.executescript(INIT_SQL)
    await _apply_chunk_column_migrations(conn)
    await conn.executescript(MIGRATION_SQL)
    await conn.commit()
    # DAG-state tables are owned by `memory_core.dag_state`. Production startup
    # applies it (see `server.lifespan` and `chunk_admin.reset_db`). Mirror that here.
    await ensure_dag_state_schema(conn)
    # The engine_lock table is owned by `memory_core.engine_lock` — same deal.
    await ensure_engine_lock_schema(conn)


def apply_runtime_schema_sync(path: str) -> None:
    """Apply the production SQLite schema to a file-backed DB via sync sqlite3.

    Used by tests that exercise the synchronous ``memory_core.dag_state`` API.
    Mirrors production's three ordered steps (INIT_SQL → additive column ALTERs →
    MIGRATION_SQL); ``_apply_chunk_column_migrations`` is async-only, so the same
    shared migration tuples are applied here through a synchronous pass.
    """
    from estormi_server.sql.schema import INIT_SQL, MIGRATION_SQL
    from estormi_server.sql.schema_columns import (
        CHUNK_COLUMN_MIGRATIONS,
        WHATSAPP_CHATS_COLUMN_MIGRATIONS,
    )
    from memory_core.dag_state import DAG_STATE_SCHEMA
    from memory_core.engine_lock import ENGINE_LOCK_SCHEMA

    conn = sqlite3.connect(path)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.executescript(INIT_SQL)
        _apply_column_migrations_sync(conn, "chunks", CHUNK_COLUMN_MIGRATIONS)
        _apply_column_migrations_sync(conn, "whatsapp_chats", WHATSAPP_CHATS_COLUMN_MIGRATIONS)
        conn.executescript(MIGRATION_SQL)
        # DAG-state tables are owned by `memory_core.dag_state`.
        conn.executescript(DAG_STATE_SCHEMA)
        # The engine_lock table is owned by `memory_core.engine_lock`.
        conn.executescript(ENGINE_LOCK_SCHEMA)
        conn.commit()
    finally:
        conn.close()


def _apply_column_migrations_sync(
    conn: sqlite3.Connection,
    table: str,
    migrations: tuple[tuple[str, str], ...],
) -> None:
    """Synchronous mirror of ``schema_columns._apply_column_migrations``.

    Adds only the absent columns (SQLite has no idempotent ADD COLUMN). ``table``
    is an internal constant, never user input — interpolating it is safe.
    """
    cur = conn.execute(f"PRAGMA table_info({table})")
    existing = {row[1] for row in cur.fetchall()}  # row[1] = column name
    if not existing:  # table absent — nothing to migrate
        return
    for name, decl in migrations:
        if name in existing:
            continue
        try:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
        except sqlite3.OperationalError as exc:
            if "duplicate column name" in str(exc).lower():
                continue
            raise
