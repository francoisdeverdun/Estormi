# 10. `main` is protected; changes land via reviewed PRs

- Status: Accepted

## Context

The repository is public. Direct commits to `main` bypass review and make
history hard to audit.

## Decision

`main` never takes a direct commit or push. Work happens on a feature branch (a
git worktree per branch for parallel sessions) and lands through a reviewed pull
request; the maintainer merges. A local `.githooks/pre-push` backstop blocks
direct pushes, and the server-side rule is applied once by
`scripts/setup_branch_protection.sh`.

## Consequences

Every change is reviewed and history stays clean. Tooling assumes this — release
tagging and badge commits flow through the PR process. A documented maintainer
override (`ESTORMI_ALLOW_MAIN_PUSH=1`) exists for the rare case the maintainer
must push directly; it is the exception, not the path.
