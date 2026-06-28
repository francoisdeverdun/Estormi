# 8. First-party Python under `packages/` with the `estormi_` naming law

- Status: Accepted

## Context

Directory names drift without a rule, and a reader should be able to tell a
deployable surface from a shared library, and an engine from a pure library, at
a glance.

## Decision

Three buckets, one location-and-case rule each — read the category off the name:

| Category | Location | Case | Naming rule | Examples |
| --- | --- | --- | --- | --- |
| Engine/server package | `packages/` | `snake_case` | `estormi_` prefix | `estormi_server`, `estormi_ingestion`, `estormi_briefing`, `estormi_distill` |
| Pure-library package | `packages/` | `snake_case` | bare name | `memory_core`, `connectors` |
| Native deployable surface | `apps/` | dash-case | product name | `estormi-macos`, `estormi-ios`, `estormi-cloud` |

`packages/` is the single workspace for shared libraries in **both** languages —
the six first-party Python packages above sit there beside the JS workspaces
(`packages/web-ui`, `packages/ui-kit`).

## Consequences

Because `packages/` deliberately mixes Python and JS, `pnpm-workspace.yaml`
enumerates the JS members explicitly instead of globbing `packages/*`. The
rename is guarded by the contract tests under `tests/contract/` —
`test_no_legacy_path_refs.py` fails the build if the old package *path* forms
(`mcp-server` or `ingestion` written as a directory, i.e. followed by a slash)
reappear in the tree — the bare words `mcp-server` and `ingestion` are
intentionally allowed, since they name the task-scoped skills under
`.claude/skills/`. The trade-off accepted: a
reader cannot infer a package's language purely from its location — judged worth
it for the single-workspace simplicity.
