# Estormi — Repository Guide

This repository is the monorepo for **Estormi — Ars Memoriae**, a local-first
personal memory app.

## Skills

Task-scoped **skills** are the only `SKILL.md` files: they live in
`.claude/skills/<name>/SKILL.md` (carry frontmatter, load on demand). A
directory's own orientation guide is a plain `README.md`, never a `SKILL.md`.
Consult the skill matching the work instead of re-deriving the layout:

- `mcp-server` — the FastAPI + MCP backend (`packages/estormi_server/`).
- `ingestion` — data-source pipelines (`packages/estormi_ingestion/`).
- `infra` — scripts, the ingestion pipeline, Makefile, release workflow, rebuild policy.
- `testing` — the pytest suite and its fixtures.
- `web-ui` — the Ars Memoriae SPA (`packages/web-ui/`).
- `mobile` — the native iOS companion (`apps/estormi-ios/`).
- `graphify` — navigable knowledge graph for cross-file lookups (`graphify-out/graph.json`).

**Default to graphify for code navigation — it saves context tokens.** For
"where is X", "who calls Y", or tracing data flow across the engines, run
`graphify query` first instead of fanning out `grep`/`Read` across the tree:
it answers from the prebuilt graph in a fraction of the tokens, then you open
only the specific files it points to. The graph is kept current by
`.githooks/pre-commit`. Fall back to grep/Read when no graph exists yet or for
exact-string matches.

## Core values

- **Boy Scout Rule** — always leave the code cleaner than you found it. When
  you touch a file, opportunistically fix what is cheap and in scope: delete
  dead code and unused imports, drop stale comments, correct misleading
  names, tighten obvious rough edges. Don't balloon a change into an
  unrelated rewrite — but never knowingly walk past rot you could have
  removed. Dead code is a liability: an unused module, a stub nothing
  imports, a test for code no one ships — remove it rather than leave it to
  mislead the next reader.

## Surfaces

- **Estormi for macOS** — packaged Tauri app (`apps/estormi-macos/`, bundle
  identifier `app.estormi.local`).
- **Estormi mobile** — native SwiftUI iOS companion at
  `apps/estormi-ios/`. A read-only viewer with two pages — Briefings and
  Metrics — reading from a folder the user keeps in iCloud Drive, except for
  the APNs device-token file it writes into the vault for the Mac to fan out
  new-briefing pushes. The vault carries `briefings/<date>.json` and an
  engine-history log; all ingestion and briefing composition happen on the
  Mac.

Estormi targets macOS and iOS only — connectors run on the Mac, the iOS app
is a read-only viewer.

## Layering

The contributor-facing code map and the layering DAG (with the import-linter
invariants drawn out) live in [`ARCHITECTURE.md`](ARCHITECTURE.md).

```
apps/estormi-macos/ (macOS Tauri shell) + apps/estormi-ios/ (native SwiftUI iOS companion) ─→ packages/estormi_server/ (FastAPI HTTP + MCP, SQLite chunks + Qdrant) ─→ memory_core (domain/support)
```

`packages/estormi_server/` is the live server: the FastAPI HTTP API, the MCP
transport, and the **two daily engines** — **Ingestion** (the daily ingestion pipeline) and
**Briefing** (a scheduled, provider-switchable composition). Ingestion builds
and structures the memory; Briefing turns it into the daily briefing.
Correlation is not a stored engine: it is emergent from time-window retrieval —
every chunk carries an accurate `date_ts` and a `corpus` tag (`personal` |
`world`), and the `fetch_around` MCP tool returns the chunks overlapping a
window. An engine mutex in `packages/estormi_server/server/jobs.py` runs only one engine at a
time. The SQLite chunk store and the Qdrant vectors are managed in
`packages/estormi_server/storage/` (`tools.py`, `qdrant_helpers.py`, `search_api.py`); it builds on
`memory_core`, the pure domain/support layer (settings, DAG-run state,
embeddings, sanitizer, audit). Full reference:
`docs/architecture/engines.md`.

`connectors` is the extensible per-source adapter layer
(`ShellConnector` / `ScriptConnector` bases,
`ConnectorRegistry` enforces unique specs); the ingestion scripts it drives
live under `packages/estormi_ingestion/`. See `docs/connectors.md`.

`packages/estormi_ingestion/` sits beside `packages/estormi_server/` in the chain: its per-source
scripts and shared chunking are driven by `connectors`, and
`packages/estormi_server/` drives the daily pipeline from it. The Briefing engine lives
in its own package `packages/estormi_briefing/` (entrypoint
`packages/estormi_briefing/run_briefing.py`, launched as a subprocess by
`packages/estormi_server/server/launchers/briefing.py`); it has several one-way edges
down into `packages/estormi_ingestion/` — it reads the `world` corpus back through
`estormi_ingestion.knowledge.ingest_world` (the connector that ingests the
world corpus and stays in `packages/estormi_ingestion/`) and pulls shared helpers from
`estormi_ingestion.shared.delivery.vault_sync` and `estormi_ingestion.shared.*`
(`config`, `paths`). All of these
are descendant/acyclic; none ever reaches up into `packages/estormi_server/`. Both
`packages/estormi_ingestion/` and `packages/estormi_briefing/` depend
on `memory_core` but never reach up into `packages/estormi_server/`.

`packages/estormi_distill/` is the **optional third engine** (Apple Silicon only): it
periodically retrains the local prose model on the user's own briefing archive
(every briefing in the vault, including hand-edited ones) and runs through the
same run-queue and engine mutex (`ENGINES = ("ingestion", "briefing",
"distill")` in `packages/estormi_server/server/jobs.py`), but sits off the
daily path — a briefing composes identically whether or not it has ever run.
Entrypoint `packages/estormi_distill/run_distill.py`, launched by
`packages/estormi_server/server/launchers/distill.py`. See
`docs/architecture/distillation.md`.

## Naming conventions

One rule, so directory names stop drifting:

- **First-party Python** lives under `packages/` in `snake_case`, beside the JS
  workspaces. Engine/server packages carry the `estormi_` prefix
  (`packages/estormi_server/`, `packages/estormi_ingestion/`,
  `packages/estormi_briefing/`, `packages/estormi_distill/`); the pure library
  packages are bare (`packages/memory_core/`, `packages/connectors/`).
  `packages/` is the **single workspace for shared libraries — Python and JS**
  (the JS workspace members are `web-ui` and `ui-kit`). Brand artwork and
  webfonts are not packages — they live in repo-root `assets/`.
- **Native deployable surfaces** live under `apps/` with dash-case product
  names: `apps/estormi-macos` (the Tauri shell — its Cargo crate keeps Tauri's
  conventional inner `src/` + `tauri.conf.json` layout), `apps/estormi-ios`, and
  `apps/estormi-cloud`. Because the shell sits one level deeper than the repo
  root, its `tauri.conf.json` bundles resources with `../../` paths (encoded as
  `_up_/_up_/` in the `.app`); `main.rs` resolves them at that depth.
- A helper directory matches its built product name (`apps/estormi-cloud/` →
  `EstormiCloud.app`).

## Branding

- Public product name: **Estormi**.
- Public tagline: **Ars Memoriae**.

## Hard rules

- **Branch discipline (public repo).** `main` is protected and never takes a
  direct commit or push. Always work on a feature branch and land changes
  through a reviewed PR — the maintainer merges. For parallel sessions on one
  machine, use a git worktree per branch so checkouts never collide. The local
  backstop is `.githooks/pre-push`; the server-side rule is applied once by
  `scripts/setup_branch_protection.sh`.
- Do not put FastAPI routes in `memory_core` — it is the pure
  storage/retrieval layer; HTTP belongs in `packages/estormi_server/`.
- Do not duplicate connector logic across apps — `packages/connectors/` is
  the single source.
- Do not hardcode user-specific paths — go through `ESTORMI_REPO_ROOT` or
  the bundle-resource resolution in `packages/estormi_server/server/jobs.py`. The app runs
  both as a relocatable `.app` bundle (resources under `_up_/`) and from a dev
  checkout, so an absolute path breaks one of the two.
- Skill `SKILL.md` files (under `.claude/skills/`) and directory `README.md`
  guides must cite docs and code, never restate them; CI's contract tests fail
  if any of them references a path or symbol that doesn't exist
  (`tests/contract/test_skill_md_references.py`; `make test` runs it locally).
