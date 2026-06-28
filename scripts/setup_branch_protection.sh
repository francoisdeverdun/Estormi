#!/usr/bin/env bash
# Apply GitHub branch protection to `main` once Estormi is public.
#
# Run ONCE after the repository is pushed to GitHub and made public. Branch
# protection is free on public repos and is the server-side half of Estormi's
# branch rule (the local half is .githooks/pre-push). It enforces that `main`
# only ever moves through a reviewed pull request.
#
# Requires: gh CLI authenticated with admin rights on the repo.
#   ./scripts/setup_branch_protection.sh <owner>/<repo>
set -euo pipefail

REPO="${1:?usage: setup_branch_protection.sh <owner>/<repo>}"

gh api -X PUT "repos/${REPO}/branches/main/protection" \
    -H "Accept: application/vnd.github+json" \
    --input - <<'JSON'
{
  "required_status_checks": null,
  "enforce_admins": true,
  "required_pull_request_reviews": { "required_approving_review_count": 0 },
  "restrictions": null,
  "allow_force_pushes": false,
  "allow_deletions": false,
  "required_linear_history": true,
  "required_conversation_resolution": true
}
JSON

echo "✓ Branch protection applied to ${REPO} main (PR-only, no force-push, no deletion)."
