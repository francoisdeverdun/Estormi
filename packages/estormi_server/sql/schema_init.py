"""Canonical SQLite DDL — all base tables, views, and the indexes that can be
created up front.

Applied idempotently on every startup (``CREATE TABLE/INDEX IF NOT EXISTS``).
Indexes on columns that :mod:`schema_columns` may add live in
:mod:`schema_migrations`, not here, because those columns don't exist until the
chunk-column pass has run. Do NOT re-declare a live table in the migration
block "to be safe" — that duplication is exactly how the base schema and the
migrations once drifted apart.
"""

from __future__ import annotations

# Full SQLite DDL — all tables. Created idempotently on each startup.
INIT_SQL = """
CREATE TABLE IF NOT EXISTS chunks (
    id            TEXT PRIMARY KEY,
    content_hash  TEXT UNIQUE NOT NULL,
    source        TEXT,
    source_id     TEXT,
    title         TEXT,
    date          TEXT,
    date_ts       TEXT,
    end_date_ts   TEXT,
    group_type    TEXT,
    pending_reply INTEGER DEFAULT 0,
    chat_id_raw   TEXT,
    completed     INTEGER DEFAULT 0,
    corpus        TEXT,
    event_type      TEXT,
    event_status    TEXT,
    working_location TEXT,
    -- WhatsApp structural kind (dm/group/broadcast), derived from the JID.
    -- Orthogonal to group_type, which is the *semantic* life-context tag.
    chat_kind     TEXT,
    ingested_at   TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS chunks_source_idx     ON chunks(source);
CREATE INDEX IF NOT EXISTS chunks_source_id_idx  ON chunks(source_id);
CREATE INDEX IF NOT EXISTS chunks_date_ts_idx    ON chunks(date_ts);
CREATE INDEX IF NOT EXISTS chunks_ingested_idx   ON chunks(ingested_at);
-- Indexes on `end_date_ts`, `group_type`, `pending_reply` and `corpus` live in
-- MIGRATION_SQL: those columns may be added by CHUNK_COLUMN_MIGRATIONS, which
-- runs *after* this script, so an index on them cannot be created here.

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ingestion_watermarks (
    source          TEXT PRIMARY KEY,
    last_fetched_at TEXT,
    last_item_id    TEXT
);

CREATE TABLE IF NOT EXISTS whatsapp_chats (
    chat_id    TEXT PRIMARY KEY,
    chat_name  TEXT,
    group_type TEXT NOT NULL DEFAULT 'unknown',
    -- Structural kind (dm/group/broadcast) derived from the JID; separate from
    -- the semantic group_type tag.
    chat_kind  TEXT,
    first_seen TEXT DEFAULT (datetime('now')),
    last_seen  TEXT DEFAULT (datetime('now'))
);

-- Durable, replayable WhatsApp message log. The bridge can only ever PUSH new
-- messages (offline queue on reconnect — there is no "fetch since timestamp"
-- in the protocol), and the server never re-delivers an acked message, so once
-- a message is gone from the offline queue it is gone. This table is the local
-- source of truth: ingestion appends each staged message here (raw, un-redacted
-- text, so re-chunking / re-embedding can replay without re-fetching) and then
-- derives `whatsapp` chunks from it by a timestamp watermark. PII redaction is
-- applied downstream at chunk time, not here. Bounded by a retention sweep
-- (WHATSAPP_LOG_RETENTION_DAYS). See estormi_ingestion/whatsapp/ingest_conversations.py.
CREATE TABLE IF NOT EXISTS whatsapp_messages (
    msg_id      TEXT PRIMARY KEY,
    chat_id     TEXT NOT NULL,
    chat_name   TEXT,
    sender_name TEXT,
    ts_iso      TEXT NOT NULL,
    text        TEXT NOT NULL,
    archived_at TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS whatsapp_messages_ts_idx   ON whatsapp_messages(ts_iso);
CREATE INDEX IF NOT EXISTS whatsapp_messages_chat_idx ON whatsapp_messages(chat_id, ts_iso);

-- One row per Briefing engine invocation. Until this table was added the only
-- structured trace of a briefing run was three free-form `settings` rows
-- (`knowledge_last_run_at` / `_status` / `_summary`), which kept the
-- BriefingPulse from charting anything meaningful. The columns are the three
-- families the pulse renders: timing (started_at/finished_at/duration_ms),
-- LLM cost (model/tokens_in/tokens_out), and composition (sections_json
-- mapping section name → item count, items_considered, items_included).
CREATE TABLE IF NOT EXISTS briefing_runs (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at           TEXT NOT NULL,
    finished_at          TEXT,
    status               TEXT,
    duration_ms          INTEGER,
    model                TEXT,
    tokens_in            INTEGER,
    tokens_out           INTEGER,
    sections_json        TEXT,
    items_considered     INTEGER,
    items_included       INTEGER,
    summary              TEXT
);
CREATE INDEX IF NOT EXISTS briefing_runs_started_idx ON briefing_runs(started_at);

-- ``dag_runs`` / ``dag_stages`` are owned solely by ``memory_core.dag_state``
-- (its ``ensure_schema`` is applied during startup / ``reset_db``).
"""
