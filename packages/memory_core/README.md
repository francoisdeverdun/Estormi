# memory_core

Pure business logic. Zero knowledge of FastAPI, MCP, Tauri, or React.

## Modules

| Module | Responsibility |
| --- | --- |
| `settings.py` | Env-driven config defaults; re-exports `resolve_data_dir` from `datadir` for backward-compatible imports. |
| `datadir.py` | Single source of truth for the library location (`resolve_data_dir`): `ESTORMI_DATA_DIR` env → relocation pointer file → default config home, plus the crash-safe, idempotent `bootstrap_relocate`. |
| `timeparse.py` | Shared ISO-8601 parsing (`parse_iso`) and the two "now" formatters (`now_iso`, `now_iso_z`). |
| `labels.py` | Shared opaque-conversation-label heuristic (`is_opaque_label`). |
| `dag_state.py` | DAG run/stage state persistence (first-class SQLite), exposing the `python -m memory_core.dag_state` CLI (`start-run`/`finish-run`/`start-stage`/`finish-stage`) that `scripts/daily_ingestion.sh` calls to record lifecycle events between stages. |
| `embedder.py` | fastembed dense (`nomic-embed-text-v1.5`, 768d) + sparse (BM25) embeddings, cached and async. |
| `sanitizer.py` | Anti-prompt-injection neutralization for retrieved chunks (`sanitize_chunk`, `sanitize_query`). |
| `pii_filter.py` | Email / phone / IBAN / NIR / credit-card / OTP / code-secret redaction (`filter_pii`, `is_otp_message`, `redact_code_secrets`); the text-safety primitive the ingestion connectors and the server's `/ingest_chunk` route both apply. |
| `audit.py` | Structured per-tool-call audit log (isolated structlog binding, size-bounded). |
| `llm_local.py` | In-process llama-cpp inference (Metal); the briefing's `local` provider and the Maintenance model catalog. Reads `*_model_tier` settings through an injected connection provider so it never imports up into the server layer — see [the mechanism note](#provider-injection) below. |
| `engine_lock.py` | DB-backed cross-process engine mutex (`BEGIN IMMEDIATE`); the shell DAG and the server both `acquire` it so only one engine runs at a time. |
| `resource_guard.py` | macOS memory-pressure governor (sysctl-based, no DB); sizes the LLM ladder and feeds the SPA's read-only readout. |
| `tts_local.py` | Local TTS synthesis (Voxtral via mlx-audio); writes the briefing narration `.m4a` the iOS companion reads from the vault. |
| `prompt_templates.py` | Shared Jinja2 environment (`render`) for `prompts/llm/*.j2`. |

### Provider injection

`llm_local` needs the selected `*_model_tier` settings value, but it must not
import the server. So `set_settings_conn_provider` lets a caller inject a SQLite
connection: `packages/estormi_server/storage/tools.py` wires the server's
connection in; absent that, `llm_local` falls back to a read-only connection on
`settings.DB_PATH`. See the `mcp-server` skill for how the server tiers wire up.
