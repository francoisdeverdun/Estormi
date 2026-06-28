"""Regression: chunks-table migrations must actually run on legacy DBs.

The previous implementation used `ALTER TABLE chunks ADD COLUMN IF NOT EXISTS`
which is **not** valid SQLite syntax. Every ALTER raised and was silently
swallowed by a bare ``except``, so legacy databases never received the new
columns (`group_type`, `pending_reply`, `chat_id_raw`, `completed`,
`end_date_ts`) and subsequent INSERTs blew up. This test recreates a legacy
schema and verifies the live migration helper populates it correctly.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import aiosqlite
import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def _make_legacy_db(path: Path) -> None:
    db = sqlite3.connect(path)
    db.executescript(
        """
        CREATE TABLE chunks (
            id           TEXT PRIMARY KEY,
            content_hash TEXT UNIQUE NOT NULL,
            source       TEXT,
            source_id    TEXT,
            title        TEXT,
            date         TEXT,
            date_ts      TEXT,
            ingested_at  TEXT DEFAULT (datetime('now'))
        );
        """
    )
    db.commit()
    db.close()


@pytest.mark.unit
async def test_legacy_chunks_table_receives_new_columns(tmp_path):
    """A pre-migration chunks table must gain all the new columns."""
    from estormi_server.sql.schema import (  # type: ignore[import]
        CHUNK_COLUMN_MIGRATIONS,
        _apply_chunk_column_migrations,
    )

    db_path = tmp_path / "legacy.db"
    _make_legacy_db(db_path)

    async with aiosqlite.connect(str(db_path)) as conn:
        await _apply_chunk_column_migrations(conn)
        await conn.commit()
        async with conn.execute("PRAGMA table_info(chunks)") as cur:
            columns = {row[1] for row in await cur.fetchall()}

    for name, _decl in CHUNK_COLUMN_MIGRATIONS:
        assert name in columns, f"migration did not add column {name!r}"


@pytest.mark.regression
async def test_full_schema_helper_upgrades_legacy_db(tmp_path):
    """The whole apply_runtime_schema sequence must succeed on an OLD chunks table.

    This guards the test helper itself, not just the column pass in isolation.
    INIT_SQL + MIGRATION_SQL alone raise ``no such column: end_date_ts`` when
    MIGRATION_SQL builds the ``chunks(end_date_ts)`` index over a legacy table.
    The helper now mirrors production (INIT_SQL → additive column ALTERs →
    MIGRATION_SQL), so the upgrade path completes and every additive column
    lands — proving fixture DBs and an upgrade-from-old DB share one code path.
    """
    from estormi_server.sql.schema import CHUNK_COLUMN_MIGRATIONS  # type: ignore[import]
    from tests.helpers.database import apply_runtime_schema

    db_path = tmp_path / "legacy.db"
    _make_legacy_db(db_path)

    async with aiosqlite.connect(str(db_path)) as conn:
        # Must not raise: the omitted column pass used to fail MIGRATION_SQL's
        # chunks(end_date_ts) index on the legacy table.
        await apply_runtime_schema(conn)
        async with conn.execute("PRAGMA table_info(chunks)") as cur:
            columns = {row[1] for row in await cur.fetchall()}

    for name, _decl in CHUNK_COLUMN_MIGRATIONS:
        assert name in columns, f"upgrade path did not add column {name!r}"


@pytest.mark.unit
async def test_migration_is_idempotent(tmp_path):
    """Running the migration twice must not raise and must converge on the same schema."""
    from estormi_server.sql.schema import (  # type: ignore[import]
        CHUNK_COLUMN_MIGRATIONS,
        _apply_chunk_column_migrations,
    )

    db_path = tmp_path / "legacy.db"
    _make_legacy_db(db_path)

    async with aiosqlite.connect(str(db_path)) as conn:
        await _apply_chunk_column_migrations(conn)
        await _apply_chunk_column_migrations(conn)  # must be a no-op
        await conn.commit()
        async with conn.execute("PRAGMA table_info(chunks)") as cur:
            columns = {row[1] for row in await cur.fetchall()}

    # Second apply must not have dropped or duplicated the migrated columns.
    for name, _decl in CHUNK_COLUMN_MIGRATIONS:
        assert name in columns, f"column {name!r} missing after idempotent re-apply"
