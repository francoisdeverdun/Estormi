# 4. MCP as a first-class transport

- Status: Accepted

## Context

Given that correlation emerges from retrieval (see
[0002](0002-collapse-precomputation-engines.md)), the model in front *is* the
correlation engine — so it needs direct access to the retrieval primitives.

## Decision

Expose memory to Claude Desktop and Claude Code as MCP tools (`search_memory`,
`fetch_around`, `ingest_chunk`, …) alongside the HTTP API. The `TOOLS` catalogue
and dispatcher live in `packages/estormi_server/api/mcp_rpc.py`.

## Consequences

The same retrieval primitives serve both the in-app UI and an external
assistant. The cost is two surfaces (HTTP + MCP) over one storage layer to keep
in sync — contained by routing both through the shared core functions in
`packages/estormi_server/storage/tools.py` and
`packages/estormi_server/storage/search_api.py`, which build on `memory_core`.
The MCP tool surface is a cross-process contract, so it is golden-snapshotted by
`tests/contract/test_mcp_tool_surface.py`.
