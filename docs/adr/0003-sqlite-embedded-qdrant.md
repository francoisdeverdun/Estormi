# 3. SQLite + embedded Qdrant for storage

- Status: Accepted

## Context

A local-first, single-user app needs durable relational metadata and semantic
vector search without asking the user to install or secure a database service.

## Decision

Chunk metadata and settings live in SQLite (`aiosqlite`); semantic vectors live
in an **embedded** Qdrant. Both are file-backed under `$ESTORMI_DATA_DIR`.

## Consequences

Both run in-process with no daemon to install, run, or secure — exactly what a
single-user, local-first, loopback app wants. SQLite gives durable relational
metadata; embedded Qdrant gives the hybrid dense+sparse (BM25) search without a
vector-database service. The trade-off is single-writer concurrency (one shared
`aiosqlite` connection in WAL mode, with writers serialised by an
`asyncio.Lock`) and no horizontal scaling — neither matters for one user.
