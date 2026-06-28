"""Additive column migrations for the Estormi DB.

SQLite has no ``ADD COLUMN IF NOT EXISTS``, so these columns are applied by
:func:`_apply_chunk_column_migrations`, which probes ``PRAGMA table_info`` and
ALTERs only the absent ones. This pass runs *before*
:data:`schema_migrations.MIGRATION_SQL` so the indexes and one-off backfills
there can reference the new columns (e.g. ``chat_kind``).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only
    import aiosqlite


# Idempotent additive ALTERs for the ``chunks`` table.
CHUNK_COLUMN_MIGRATIONS: tuple[tuple[str, str], ...] = (
    ("group_type", "TEXT"),
    ("pending_reply", "INTEGER DEFAULT 0"),
    ("chat_id_raw", "TEXT"),
    ("completed", "INTEGER DEFAULT 0"),
    ("end_date_ts", "TEXT"),
    ("corpus", "TEXT"),
    # Calendar event facts the Briefing reads as structured fields rather than
    # parsing them back out of the chunk text (see estormi_ingestion/google_calendar):
    #   event_type       — default / outOfOffice / focusTime (Google `eventType`)
    #   event_status     — confirmed / tentative (a "maybe" RSVP)
    #   working_location — the day's working location label, e.g. "Home office"
    ("event_type", "TEXT"),
    ("event_status", "TEXT"),
    ("working_location", "TEXT"),
    # WhatsApp structural kind (dm/group/broadcast), split out of group_type.
    ("chat_kind", "TEXT"),
)

# Additive columns for the whatsapp_chats table (same idempotent-ALTER pass).
WHATSAPP_CHATS_COLUMN_MIGRATIONS: tuple[tuple[str, str], ...] = (("chat_kind", "TEXT"),)


async def _apply_column_migrations(
    conn: aiosqlite.Connection,
    table: str,
    migrations: tuple[tuple[str, str], ...],
) -> None:
    """Add missing columns to ``table`` (SQLite has no idempotent ADD COLUMN).

    Probes the live schema via PRAGMA table_info and issues ALTER only for
    columns that are actually absent. Re-raises anything other than the
    well-known "duplicate column name" race so real schema corruption is
    surfaced rather than silently swallowed. ``table`` is an internal
    constant, never user input — interpolating it is safe.
    """
    import aiosqlite as _aiosqlite  # noqa: PLC0415

    async with conn.execute(f"PRAGMA table_info({table})") as cur:
        existing = {row[1] for row in await cur.fetchall()}  # row[1] = column name
    if not existing:
        # Table absent (PRAGMA table_info returns no rows). Production creates
        # every table in INIT_SQL before this pass runs, so a missing table here
        # means a caller passed one this DB doesn't have — ALTERing it would
        # raise "no such table". Nothing to migrate; skip.
        return
    for name, decl in migrations:
        if name in existing:
            continue
        try:
            await conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
        except _aiosqlite.OperationalError as exc:
            if "duplicate column name" in str(exc).lower():
                continue
            raise


async def _apply_chunk_column_migrations(conn: aiosqlite.Connection) -> None:
    """Add the additive columns for ``chunks`` and ``whatsapp_chats``.

    Runs before ``MIGRATION_SQL`` so the indexes and one-off backfills there can
    reference the new columns (e.g. ``chat_kind``). See
    :func:`_apply_column_migrations`.
    """
    await _apply_column_migrations(conn, "chunks", CHUNK_COLUMN_MIGRATIONS)
    await _apply_column_migrations(conn, "whatsapp_chats", WHATSAPP_CHATS_COLUMN_MIGRATIONS)
