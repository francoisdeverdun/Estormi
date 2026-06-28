<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="../assets/brand/estormi-wordmark-dark.svg">
    <img src="../assets/brand/estormi-wordmark-light.svg" alt="Estormi" width="220">
  </picture>
</p>

<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="../assets/brand/estormi-divider.svg">
    <img src="../assets/brand/estormi-divider-light.svg" alt="" width="420">
  </picture>
</p>

# Estormi as a memory server (MCP)

Estormi is two products in one process. The first is the app: the macOS shell,
the searchable archive, and the daily Briefing. The **second feature** is the
one this page is about — Estormi is a local **MCP server**, so an AI assistant
(Claude Code, Claude Desktop, or any MCP-capable client) can query the memory
you have already built, right from a conversation, without that data ever
leaving your Mac.

Where a normal chat only knows what you paste into it, an assistant wired to
Estormi can ask *your own archive* — "what did I decide about X?", "pull
everything around last Tuesday", "is there prior context on this?" — and get
back chunks drawn from your notes, mail, messages, calendar, documents, and
health data.

> The server is the same FastAPI process that powers the app. There is nothing
> extra to run: if Estormi is open (or the dev server is up), the MCP endpoint
> is live.

## Endpoint & transport

The MCP transport is implemented in
[`packages/estormi_server/api/mcp_rpc.py`](../packages/estormi_server/api/mcp_rpc.py):

| Route | Protocol | Purpose |
|---|---|---|
| `POST /mcp` | JSON-RPC 2.0 | `initialize`, `tools/list`, `tools/call` |
| `GET /sse` | Server-Sent Events | push-notification stream |

The server binds to **`127.0.0.1:8000`** — reachable only from the same Mac.
Override the bind with `MCP_SERVER_HOST` / `MCP_SERVER_PORT` before startup
(source checkouts only; `make start` reads both and passes them to uvicorn).

## The tool catalog

The catalog is defined in `mcp_rpc.py` (the `TOOLS` list) and is what an
assistant sees on `tools/list`:

| Tool | What it does |
|---|---|
| `search_memory` | Hybrid (dense + BM25) search over the archive, with source / date / corpus filters and an optional dense-cosine relatedness floor. |
| `fetch_around` | Time-window retrieval: every chunk across all sources whose date overlaps a window centred on a date — the correlation primitive. Supports an asymmetric `forward_days` to keep the window from crossing into tomorrow. |
| `get_chunk` | Fetch one chunk by id. |
| `ingest_chunk` | Store a text chunk in the archive (used by the ingestion pipelines and by tools that want to write memory). |
| `delete_chunk` | Delete one chunk by id. |
| `delete_by_source` | Delete every chunk from a given source. |

The same two read tools are exposed as plain REST shims (`POST /search_memory`,
`POST /fetch_around`) for clients that prefer REST to JSON-RPC — see
[`packages/estormi_server/api/search.py`](../packages/estormi_server/api/search.py).

### Request shape (read tools)

The two read tools take these arguments (from the `inputSchema` of each entry in
`TOOLS`); **bold** = required, everything else optional:

**`search_memory`**

| Argument | Type | Notes |
|---|---|---|
| **`query`** | string | Natural-language query. |
| `limit` | integer | Top-k, 1–100, default 10. |
| `source` | string | Single-source filter (one of the catalogued source slugs). |
| `sources` | string[] | Multi-source filter (OR); overrides `source` if both are given. |
| `corpus` | `personal` \| `world` | Scope to own memory or world news/knowledge; omit to search both. |
| `after` / `before` | string | ISO-8601 lower / upper bound on `date_ts`. |
| `group_type` | string | Semantic life-context filter: `me`, `partner`, `work`, `family`, `couple`, `friends`, `organisation`, `charity`, `sport`, `noise`, `unknown`. |
| `chat_kind` | `dm` \| `group` \| `broadcast` | Structural WhatsApp-chat filter, independent of `group_type`. |
| `pending_reply` | boolean | If true, only WhatsApp chunks the user has not yet replied to. *(Currently inert — the ingestor no longer computes the flag; see [whatsapp-rust-sidecar.md](specs/whatsapp-rust-sidecar.md).)* |
| `min_score` | number | Absolute dense-cosine floor [0,1]; when set, runs dense-only relatedness mode (~0.6 is a good floor). |

**`fetch_around`**

| Argument | Type | Notes |
|---|---|---|
| **`date`** | string | Centre of the window, ISO-8601. |
| `window_days` | integer | Half-width in days, 0–90, default 1 (window spans `date ± window_days`). |
| `forward_days` | integer | Independent look-ahead, 0–90; omit for symmetric, set `0` to stop the window crossing into tomorrow. |
| `sources` | string[] | Restrict to a subset of sources (OR). |
| `corpus` | `personal` \| `world` | Corpus scope; omit for both. |
| `limit` | integer | Max chunks, 1–500, default 200. |

## Authentication

The gate lives in
[`packages/estormi_server/server/security.py`](../packages/estormi_server/server/security.py):

- **Same-Mac (loopback).** A request that genuinely originates from `127.0.0.1`
  with a matching `Host` skips the token — the common local setup needs no
  configuration. (A spoofed `Host` header never gets the loopback skip.)
- **Anything else.** A bearer token is required. Set it with the
  `ESTORMI_MCP_TOKEN` environment variable, or store it in the macOS keychain
  under service `estormi`, account `mcp_token`. Clients then send
  `Authorization: Bearer <token>`.

Only configure a token if you expose the server beyond loopback (e.g. behind a
reverse proxy). For a single Mac, the default loopback setup is enough.

## Connecting a client

### Quick check (curl)

List the tools the server offers:

```bash
curl -s http://127.0.0.1:8000/mcp \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' | jq '.result.tools[].name'
```

Call one — pull the time-window around a date:

```bash
curl -s http://127.0.0.1:8000/mcp \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/call",
       "params":{"name":"fetch_around","arguments":{"date":"2026-06-14","window_days":2,"forward_days":0}}}'
```

### Claude Code

Add Estormi as an HTTP MCP server:

```bash
claude mcp add estormi --transport http http://127.0.0.1:8000/mcp
# if you set a token:
claude mcp add estormi --transport http http://127.0.0.1:8000/mcp \
  --header "Authorization: Bearer $ESTORMI_MCP_TOKEN"
```

The tools then appear as `mcp__estormi__search_memory`, `…__fetch_around`, etc.

### Claude Desktop

Add Estormi as a custom connector pointing at `http://127.0.0.1:8000/mcp`
(HTTP transport). On clients that only speak the stdio transport, bridge with
[`mcp-remote`](https://www.npmjs.com/package/mcp-remote):

```json
{
  "mcpServers": {
    "estormi": {
      "command": "npx",
      "args": ["-y", "mcp-remote", "http://127.0.0.1:8000/mcp"]
    }
  }
}
```

## Privacy

Every tool runs against the local SQLite + Qdrant store on your Mac. The MCP
server reads and writes that store directly; it does not call out to any cloud.
Connecting an assistant lets *it* read your memory over loopback — the archive
itself stays on the machine. See [SECURITY.md](../.github/SECURITY.md) for the full
network-egress picture.
