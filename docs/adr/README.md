# Architecture Decision Records

This directory holds Estormi's **Architecture Decision Records (ADRs)** — one
file per load-bearing decision, each recording the *why* and the trade-off so a
choice can be revisited deliberately rather than re-argued by accident.

Format is lightweight [MADR](https://adr.github.io/madr/): **Status → Context →
Decision → Consequences**. Add a new numbered file when a decision is the kind
you would otherwise have to re-explain. Existing ADRs are not rewritten when
superseded — add a new one that supersedes them and update the Status.

For the narrative overview that ties these together, see
[`../architecture/rationale.md`](../architecture/rationale.md); for *what* the
components are and *how* they run, see
[`../architecture/overview.md`](../architecture/overview.md) and
[`../architecture/engines.md`](../architecture/engines.md).

| ADR | Decision | Status |
|---|---|---|
| [0001](0001-local-first-loopback-only.md) | Local-first, loopback-only | Accepted |
| [0002](0002-collapse-precomputation-engines.md) | Collapse precomputation engines; correlation emerges from retrieval | Accepted |
| [0003](0003-sqlite-embedded-qdrant.md) | SQLite + embedded Qdrant for storage | Accepted |
| [0004](0004-mcp-first-class-transport.md) | MCP as a first-class transport | Accepted |
| [0005](0005-single-engine-mutex.md) | One heavy engine at a time (the run-queue mutex) | Accepted |
| [0006](0006-icloud-vault-for-ios.md) | iCloud Drive vault for the iOS companion | Accepted |
| [0007](0007-connectors-single-source.md) | Connectors are the single source of ingestion logic | Accepted |
| [0008](0008-python-under-packages-naming-law.md) | First-party Python under `packages/` with the `estormi_` naming law | Accepted |
| [0009](0009-layering-dag-import-linter.md) | Layering is a one-way DAG enforced by import-linter | Accepted |
| [0010](0010-main-branch-protection.md) | `main` is protected; changes land via reviewed PRs | Accepted |
