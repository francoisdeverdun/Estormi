"""Ordered SQL migrations applied on every startup, after :data:`schema_init.INIT_SQL`
and the chunk-column pass (:func:`schema_columns._apply_chunk_column_migrations`).

Three kinds only:
  1. indexes on columns :data:`schema_columns.CHUNK_COLUMN_MIGRATIONS` may add —
     they must run *after* that pass, so they cannot live in ``INIT_SQL``;
  2. a one-off backfill of the ``corpus`` tag for pre-existing rows;
  3. one-off drops of legacy schema objects (the retired Extraction and
     Correlation engines and their tables).

Anything that belongs in the base schema goes in ``INIT_SQL``. Do NOT re-declare
a live table here "to be safe" — that duplication is exactly how ``INIT_SQL`` and
this block once drifted apart.
"""

from __future__ import annotations

MIGRATION_SQL = """
CREATE INDEX IF NOT EXISTS chunks_end_date_ts_idx ON chunks(end_date_ts);
CREATE INDEX IF NOT EXISTS chunks_group_type_idx  ON chunks(group_type);
CREATE INDEX IF NOT EXISTS chunks_pending_idx     ON chunks(pending_reply);
CREATE INDEX IF NOT EXISTS chunks_corpus_date_idx ON chunks(corpus, date_ts);
CREATE INDEX IF NOT EXISTS chunks_chat_kind_idx   ON chunks(chat_kind);
-- Canonical-UTC time-window EXPRESSION indexes. fetch_around filters and sorts
-- on ``datetime(date_ts)`` (offsets normalised to UTC for correct cross-timezone
-- ordering). A plain index on the raw column can't serve that expression, so
-- that query used to full-scan + filesort. (The briefing's day_context queries
-- also wrap date_ts in datetime() but are already served by the more-selective
-- chunks_source_idx, so the win here is concentrated on fetch_around.)
-- Indexing the *expression itself* makes the planner use the index for both the
-- range filter and the ORDER BY (verified: "SEARCH ... USING INDEX
-- chunks_date_ts_utc_idx"). The index expression must match the query's byte for
-- byte. Expression indexes (not a generated column) because aiosqlite's ALTER
-- TABLE ADD COLUMN silently no-ops for generated columns on existing DBs.
CREATE INDEX IF NOT EXISTS chunks_date_ts_utc_idx     ON chunks(datetime(date_ts));
CREATE INDEX IF NOT EXISTS chunks_corpus_date_utc_idx ON chunks(corpus, datetime(date_ts));
CREATE INDEX IF NOT EXISTS chunks_end_date_ts_utc_idx
    ON chunks(datetime(COALESCE(end_date_ts, date_ts)));
-- Expression index on date(ingested_at) — the vault_metrics GROUP BY uses
-- this expression; without it the query full-scans the chunks table.
CREATE INDEX IF NOT EXISTS chunks_ingested_date_idx ON chunks(date(ingested_at));
-- World chunks (knowledge/news/rss/youtube) are tagged at ingest via
-- _corpus_for_source; this one-off backfill only fills legacy NULL-corpus
-- rows as personal.
UPDATE chunks SET corpus = 'personal' WHERE corpus IS NULL;
-- chat_kind split (one-off, idempotent): the structural kind used to be stored
-- in group_type as the JID fallback (dm/group/broadcast). Derive chat_kind from
-- the stored JID, then demote any structural value still sitting in the
-- *semantic* group_type column back to 'unknown'. Re-running is a no-op once the
-- WHERE clauses no longer match. Mirrors _chat_kind_from_jid (tools.py).
UPDATE chunks SET chat_kind = CASE
    WHEN chat_id_raw LIKE '%@g.us'           THEN 'group'
    WHEN chat_id_raw LIKE '%@s.whatsapp.net' THEN 'dm'
    WHEN chat_id_raw LIKE '%@lid'            THEN 'dm'
    WHEN chat_id_raw LIKE '%@broadcast'      THEN 'broadcast'
    ELSE chat_kind
END
WHERE source = 'whatsapp' AND chat_id_raw IS NOT NULL AND chat_kind IS NULL;
UPDATE chunks SET group_type = 'unknown'
    WHERE group_type IN ('dm', 'group', 'broadcast');
UPDATE whatsapp_chats SET chat_kind = CASE
    WHEN chat_id LIKE '%@g.us'           THEN 'group'
    WHEN chat_id LIKE '%@s.whatsapp.net' THEN 'dm'
    WHEN chat_id LIKE '%@lid'            THEN 'dm'
    WHEN chat_id LIKE '%@broadcast'      THEN 'broadcast'
    ELSE chat_kind
END
WHERE chat_kind IS NULL;
UPDATE whatsapp_chats SET group_type = 'unknown'
    WHERE group_type IN ('dm', 'group', 'broadcast');
-- Legacy tables from the retired Extraction + Correlation engines. Dropped so
-- an existing DB converges on the two-engine (Ingestion + Briefing) schema.
DROP VIEW  IF EXISTS resolved_entities;
DROP TABLE IF EXISTS entities;
DROP TABLE IF EXISTS entity_extraction_runs;
DROP TABLE IF EXISTS entity;
DROP TABLE IF EXISTS entity_annotations;
DROP TABLE IF EXISTS entity_clinic_proposals;
DROP TABLE IF EXISTS commitments;
DROP TABLE IF EXISTS decisions;
DROP TABLE IF EXISTS correlation_runs;
DROP TABLE IF EXISTS chunk_links;
DROP TABLE IF EXISTS chunk_link_runs;
DROP TABLE IF EXISTS topics;
DROP TABLE IF EXISTS topic_chunks;
DROP TABLE IF EXISTS correlations;
DROP TABLE IF EXISTS correlation_feedback;
DROP TABLE IF EXISTS correlation_meta;
DROP TABLE IF EXISTS correlation_judgments;
DROP TABLE IF EXISTS whatsapp_commitments;
DROP TABLE IF EXISTS whatsapp_processed_chunks;
-- Retired feature: per-run LLM analyses of pipeline logs. No live writer or
-- reader remains, so drop the table rather than recreate-and-clear it.
DROP TABLE IF EXISTS pipeline_ai_analyses;
-- Retired feature: chunk annotations (label/note/pin). The CRUD routes and any
-- UI for them were removed, so drop the table rather than keep it empty.
DROP TABLE IF EXISTS chunk_annotations;
"""
