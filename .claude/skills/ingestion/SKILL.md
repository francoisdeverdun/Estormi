---
name: ingestion
description: 'Develop Estormi ingestion pipelines (packages/estormi_ingestion/). USE FOR: adding or modifying data sources, chunking, PII filtering, watermarks, shell bridges, staged imports, WhatsApp conversation ingestion, the documents walker, and Briefing.'
---

# Ingestion Pipelines

## When to Use

- Adding a new source to Estormi.
- Modifying an existing source pipeline.
- Changing shared chunking, PII filtering, or watermark logic.
- Debugging `scripts/daily_ingestion.sh`.
- Working with staging directories or shell-to-Python bridges.
- Updating Briefing fetch/build/delivery behavior.

## Source Inventory

"Default nightly?" is the single source for which stages `connectors stages`
runs unattended vs. which only run under `connectors stages --all`; both flow
blocks below reference it instead of re-listing stage names.

| Source | Entry point | Stored source | Watermark style | Default nightly? | Notes |
|---|---|---|---|---|---|
| Apple Notes | `packages/estormi_ingestion/apple_notes/watch_and_ingest.sh` | `notes` | SQLite watermark | Yes | AppleScript export, chunked before ingest |
| Apple Mail | `packages/estormi_ingestion/apple_mail/watch_and_ingest.sh` | `mail` | SQLite watermark | Yes | AppleScript export, Mail must be available |
| Google Calendar | `packages/estormi_ingestion/google_calendar/sync.py` | `gcal` | Google sync-token incremental | On demand (`--all`) | `default_stage=False`; OAuth via `packages/estormi_server/api/calendar_oauth.py` |
| WHOOP | `packages/estormi_ingestion/whoop/sync.py` | `whoop` | SQLite watermark (timestamp + last cycle id) | On demand (`--all`) | `default_stage=False`; OAuth via `packages/estormi_ingestion/whoop/auth.py` — daily recovery/sleep/strain/workouts |
| Reminders | `packages/estormi_ingestion/reminders/watch_and_ingest.sh` | `reminders` | Full dump + dedup | Yes | EventKit export via bundled Python (`export_reminders.py`); shares the TCC identity primed at activation |
| iMessage | `packages/estormi_ingestion/imessage/watch_and_ingest.sh` | `imessage` | Script-managed incremental fetch | Yes | Reads a Tauri-served snapshot of `chat.db` (the main Rust binary holds FDA and copies the DB on demand via `POST /api/imessage/snapshot`; the Python ingestor reads the copy) |
| WhatsApp | `packages/estormi_ingestion/whatsapp/watch_and_ingest.sh` | `whatsapp` | Durable log + timestamp watermark | Yes | Sidecar drains the offline queue to `OfflineSyncCompleted`; ingestor appends staging → `whatsapp_messages` log, derives chunks by watermark |
| Documents | `packages/estormi_ingestion/documents/ingest_documents.py` | `documents` | SQLite watermark | Yes | Supports `--root` and dry-run behavior |
| External knowledge | `packages/estormi_ingestion/knowledge/ingest_world.py` | `knowledge` (corpus `world`) | SQLite watermark (key `knowledge`) | Yes | pipeline stage `knowledge`: fetches configured YouTube transcripts + RSS as raw `world`-corpus chunks (no LLM). `news` survives only as the `source_id` prefix. |
| Briefing | `packages/estormi_briefing/run_briefing.py` | — (vault only) | Settings status fields | No (separate engine) | Reads the `world` + `personal` corpus from the DB and composes a daily digest delivered to the iCloud vault. Not re-ingested as chunks — its source material already lives in the DB. |

## Directory Structure

```text
packages/estormi_ingestion/
├── shared/
│   ├── chunker.py           # sliding-window chunking
│   ├── config.py            # MCP-server URL single source of truth (mcp_url())
│   ├── emit.py              # shared post_chunks() chunk-emit loop
│   ├── http_client.py       # retrying httpx client for /ingest_chunk
│   ├── log_timestamps.sh    # shared bash logging helper for connector scripts
│   ├── paths.py             # data-dir / DB-path resolution (ESTORMI_DATA_DIR/DB)
│   ├── token_store.py       # OAuth token persistence (keyring-backed)
│   ├── watch_common.sh      # shared loop/staging helpers for watch_and_ingest scripts
│   ├── watermark.py
│   ├── delivery/            # vault_sync, apns_push, cloudkit_doorbell, macos_folder_icon
│   └── host/                # macos_permissions, app_lifecycle
│   # (PII redaction moved to packages/memory_core/pii_filter.py — the text-safety bottom layer)
├── apple_notes/
├── apple_mail/
├── google_calendar/      # on-demand `gcal` stage (OAuth)
├── reminders/
├── imessage/
├── whatsapp/
├── documents/
├── knowledge/
└── whoop/                # on-demand `whoop` stage (OAuth)
```

## Shared Utilities

### `chunker.py`

```python
from estormi_ingestion.shared.chunker import sliding_chunks

chunks = sliding_chunks(text, size=800, overlap=100)
```

### `watermark.py`

```python
from estormi_ingestion.shared.watermark import get_watermark, set_watermark

last_ts, last_id = await get_watermark("notes")  # returns (timestamp | None, item_id | None)
await set_watermark("notes", fetched_at, item_id=None)
```

### `http_client.py`

Wraps `httpx.post` with exponential backoff on connection errors and 5xx.
Use it instead of bare `httpx.post` — connectors used to lose whole chunks
to a single hiccup on the loopback uvicorn.

### `emit.py`

`post_chunks(source, source_id, chunks, *, mcp_url, title, ...)` is the
canonical way to ship a source's chunks: it builds the canonical
`/ingest_chunk` payload, hashes once, POSTs each chunk with
`content_hash=f"{base}-{idx}"`, and returns an `EmitCounts` (`ok / skipped /
failed`). Prefer it over hand-rolling the enumerate→payload→post loop — the
file/text connectors (documents, world) use it.

### `config.py`

`mcp_url()` is the single source of truth for the loopback MCP-server URL;
every Python ingestor resolves the endpoint through it rather than hardcoding
a host/port.

### `vault_sync.py`

Writes the daily briefing and the engine-history log as plain JSON into the
user's iCloud Drive folder ("the vault"). The iOS companion reads that folder
directly. Don't reach for CloudKit; the vault is the sync path.

## Ingestion Contract

All source ingestors eventually post chunks to:

```text
POST /ingest_chunk
```

The payload must use `text`, not `content`:

```python
await client.post(f"{MCP_URL}/ingest_chunk", json={
    "text": chunk,
    "source": "my_source",
    "source_id": record["id"],
    "title": record.get("title", ""),
    "date": record.get("date", ""),
    "content_hash": f"{stable_hash}-{idx}",
})
```

`content_hash` is required. `source_id` should be stable when the upstream item
has a durable ID or path.

## How to Add a New Data Source

1. Create `packages/estormi_ingestion/<stage>/` with a focused fetch/ingest script.
2. Use `MCP_SERVER_URL` from the environment and normalize with `.rstrip("/")`.
3. Use `sliding_chunks()` for long text unless the source needs a custom windowing strategy.
4. Generate deterministic `content_hash` values.
5. Register a connector in `packages/connectors/` (`dag_stage=True`) if it belongs in the ingestion pipeline.
6. Add Settings/Overview visibility when users need control or status.
7. Add focused tests in `tests/`.
8. Run `make test`.

## Daily Ingestion Pipeline Flow

```text
daily_ingestion.sh
  -> stage command
  -> fetch/export source data
  -> chunk and filter
  -> POST /ingest_chunk
  -> update watermark or processed marker
  -> write stage status/log
```

The nightly run is the **seven default stages** from the Source Inventory
("Default nightly? = Yes"), in `dag_order`:

```text
notes mail reminders imessage whatsapp documents knowledge
```

Stages are owned by the connector registry in `packages/connectors/`;
`daily_ingestion.sh` derives the list from `python -m connectors stages` (add
`--all` to also run the on-demand `gcal` and `whoop` stages).

The Briefing engine (`packages/estormi_briefing/run_briefing.py`) is not a pipeline
stage — it runs separately on its own cron, launched by the server under the
engine mutex. See `docs/architecture/engines.md` for the two-engine model.

## Briefing composition

How the corpus becomes one briefing is its own model — read
`docs/architecture/briefing-generation.md` before touching
`run_briefing.py`, the `prompts/llm/knowledge_*.j2` templates, or
`build_daily_note.py`. The conceptual keys to preserve:

- **Code owns facts & links; the LLM only judges & writes.** Attribution,
  ownership and selection are deterministic — never left to the model to format.
- **Correlation = retrieval, not the LLM** — anchored on *now*, unbounded in
  *time* (`_CORR_HORIZON_DAYS` / `_CORR_LOOKBACK_DAYS` +
  `packages/estormi_briefing/compose/graph.py`);
  the day-vision only narrates the pre-assembled threads.
- **Attribution / anti-hallucination in code** — `_numbered_news` +
  `resolve_news_citations`, with `fallback_news_from_items` /
  `fallback_themes_from_items` when a weak model breaks the format (never an
  empty or fabricated section).
- **Generic by construction** — code and base prompts key on structural tags
  (`group_type`, source `kind`, `working_location`) only; the only
  user-specific inputs are the UI prompts `briefing_user_context` and per-source
  `pre_prompt`. No identities or source names in `packages/estormi_ingestion/`/`prompts/`.
- **Two-quills local mode** — per-stage tier routing + decode profiles
  (`packages/estormi_briefing/llm/decode_profiles.py`), the exemplar bank
  (`packages/estormi_briefing/compose/exemplars.py`, data-dir only — personal data), and the
  per-stage A/B bench (`packages/estormi_briefing/stage_harness.py`) are documented in
  the "Two quills" section of `docs/architecture/briefing-generation.md`.
- **Distillation engine** — `packages/estormi_distill/` trains the local prose quill
  on the user's own briefing archive harvested from the vault
  (`references.harvest_archive` in `packages/estormi_distill/references.py`); read
  `docs/architecture/distillation.md` before touching it. Briefing and
  ingestion must never import it (the `[tool.importlinter]` contract in
  `pyproject.toml`).

## Critical Patterns

- Always read `MCP_SERVER_URL` from the environment.
- Prefer `source_id` values that survive reruns.
- Keep per-record failures isolated where possible.
- Do not delete staging files before successful ingestion.
- For documents, respect `--root` and skip rules.
- For WhatsApp, preserve the staged `.txt` + `.meta.json` contract.

## Known Issues

| Issue | Impact | Location |
|---|---|---|
| Apple Notes/Mail shell scripts contain inline Python | Harder to unit test | `packages/estormi_ingestion/apple_notes/`, `packages/estormi_ingestion/apple_mail/` |
| PII filtering is source-specific | Some sources may store sensitive text | Source ingestors |
| WhatsApp capture is bounded to the nightly offline-queue drain (no continuous connection — always-on suppresses phone notifications) | Drain to `OfflineSyncCompleted` + durable `whatsapp_messages` log replayable by watermark; see rationale | `docs/specs/whatsapp-rust-sidecar.md`, `apps/estormi-macos/src/whatsapp/`, `packages/estormi_ingestion/whatsapp/ingest_conversations.py` |
