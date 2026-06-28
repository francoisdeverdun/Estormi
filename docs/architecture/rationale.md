<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="../../assets/brand/estormi-wordmark-dark.svg">
    <img src="../../assets/brand/estormi-wordmark-light.svg" alt="Estormi" width="220">
  </picture>
</p>

<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="../../assets/brand/estormi-divider.svg">
    <img src="../../assets/brand/estormi-divider-light.svg" alt="" width="420">
  </picture>
</p>

# Design Rationale

Why Estormi is built the way it is. [overview.md](overview.md) describes *what*
the components are and [engines.md](engines.md) *how* they run; this page is the
narrative index of *why* each load-bearing decision was made.

Each decision is recorded as an individual **Architecture Decision Record (ADR)**
under [`../adr/`](../adr/), in MADR format (Status → Context → Decision →
Consequences) so a choice can be revisited deliberately rather than re-argued by
accident. The [ADR index](../adr/README.md) is the authoritative list of decision
*titles and status*; the table below is the *why* in one line each — read it to
find the decision you care about, then open its ADR for the full trade-off.

| # | Decision | Why it was made |
|---|---|---|
| [0001](../adr/0001-local-first-loopback-only.md) | Local-first, loopback-only | Privacy *is* the product — everything runs in-process on the Mac, FastAPI binds `127.0.0.1`, so there is no server to breach and no cloud trust to extend. |
| [0002](../adr/0002-collapse-precomputation-engines.md) | Collapse precomputation engines | Deleted the Extraction/Correlation engines and their stale precomputed tables; time is the one correlation key that never goes stale, so correlation emerges on demand from `fetch_around` retrieval. |
| [0003](../adr/0003-sqlite-embedded-qdrant.md) | SQLite + embedded Qdrant | Durable relational metadata and semantic vectors with **no daemon to install or secure** — exactly what a single-user, loopback app wants. |
| [0004](../adr/0004-mcp-first-class-transport.md) | MCP as a first-class transport | If the model in front *is* the correlation engine (0002), it needs the retrieval primitives directly — so Claude gets `search_memory`/`fetch_around`/… as MCP tools alongside the HTTP API. |
| [0005](../adr/0005-single-engine-mutex.md) | One heavy engine at a time | Ingestion, Briefing, and Distill all contend for the LLM, Qdrant, and SQLite; a FIFO run-queue mutex is simpler and safer than fine-grained locking. |
| [0006](../adr/0006-icloud-vault-for-ios.md) | iCloud Drive vault for iOS | Keeps the loopback boundary (0001): the Mac writes briefing JSON to an iCloud folder the read-only phone reads — no server exposed, no auth, no paid account for the data path. |
| [0007](../adr/0007-connectors-single-source.md) | Connectors are the single source | One `ConnectorSpec` per source in `packages/connectors/`, shared across surfaces, so adapter logic never drifts by being duplicated per app. |
| [0008](../adr/0008-python-under-packages-naming-law.md) | Python under `packages/`, `estormi_` law | A location-and-case rule so a reader tells a deployable surface from a library, and an engine from a pure library, off the name alone. |
| [0009](../adr/0009-layering-dag-import-linter.md) | Layering is a one-way DAG | Documented layering drifts; only an enforced rule cannot — `import-linter` plus AST contract tests catch any upward import at the local gate, not in review. |
| [0010](../adr/0010-main-branch-protection.md) | `main` is protected | The repo is public; every change lands via a reviewed PR so history stays auditable, with a `pre-push` backstop and a server-side rule. |

Existing ADRs are superseded, never rewritten, so each row maps one-to-one to a
current record; when a decision is revisited, a new ADR supersedes the old and
this row points at the new one.

## See also

- [overview.md](overview.md) — component map, layering, storage.
- [engines.md](engines.md) — the engines and correlation-via-retrieval.
- [migrations.md](../migrations.md) — the schema, including the dropped legacy tables.
