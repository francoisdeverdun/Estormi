# estormi_server

The live backend: the FastAPI HTTP API, the MCP transport, and the daily
engines. Everything else in `packages/` is a library this package drives. The
full skill is
[`.claude/skills/mcp-server/SKILL.md`](../../.claude/skills/mcp-server/SKILL.md)
— it covers MCP tools, REST endpoints, search/ingest, time-window retrieval, DB
schema, local-LLM calls, and server lifecycle/security.

Quick map:

- `main.py` — FastAPI app factory; mounts the API routers and the SPA.
- `api/` — HTTP route modules; `api/mcp_rpc.py` is the MCP JSON-RPC + SSE
  transport (the `TOOLS` catalogue and dispatcher).
- `server/` — runtime: the engine run-queue and mutex (`server/jobs.py`,
  `ENGINES = ("ingestion", "briefing", "distill")`) and the engine launchers
  under `server/launchers/`.
- `storage/` — the SQLite chunk store and Qdrant vectors: `storage/tools.py`,
  `storage/search_api.py` (`search_memory` / `fetch_around`), and
  `storage/qdrant_helpers.py`. Builds on `packages/memory_core/`.
- `requirements.txt` — the loose `>=` contributor floors (the single source of
  truth for runtime deps; the packaged app pins them in
  `../../requirements/requirements-bundle.txt`).

Layering rule: HTTP belongs here, never in `memory_core`. See
[`../../docs/architecture/engines.md`](../../docs/architecture/engines.md).
