#!/usr/bin/env bash
# One-shot bootstrap. Run from repo root.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

echo "=== Estormi setup ==="

if [ ! -f .env ]; then
  cp .env.example .env
  echo "→ .env copied from .env.example."
  echo "  Review it and fill in any API keys you need, then run 'make start'."
fi

# Create Python venv if missing
if [ ! -d .venv ]; then
  echo "→ Creating Python venv…"
  python3 -m venv .venv
fi

echo "→ Installing Python dependencies…"
.venv/bin/pip install -q -r packages/estormi_server/requirements.txt

echo "→ Installing local subpackages (editable)…"
.venv/bin/pip install -q -e packages/memory_core
.venv/bin/pip install -q -e packages/connectors
.venv/bin/pip install -q -e packages/estormi_ingestion
.venv/bin/pip install -q -e packages/estormi_briefing
.venv/bin/pip install -q -e packages/estormi_server
.venv/bin/pip install -q -e packages/estormi_distill

echo "→ Installing test dependencies…"
if [ -f tests/requirements-test.txt ]; then
  .venv/bin/pip install -q -r tests/requirements-test.txt
else
  .venv/bin/pip install -q pytest pytest-asyncio pytest-cov pytest-timeout
fi

# Install frontend dependencies (optional — the server works without the SPA)
if command -v pnpm >/dev/null 2>&1; then
  echo "→ Installing JS dependencies…"
  pnpm install
else
  echo "→ pnpm not found — skipping frontend install. Run 'npm i -g pnpm && pnpm install' to enable /app/."
fi

# Create data directory
DATA_DIR="${ESTORMI_DATA_DIR:-$HOME/Library/Application Support/Estormi}"
mkdir -p "$DATA_DIR/models"

# Install the graphify CLI + seed graphify-out/ so the /graphify Claude Code
# skill has a graph ready on first invocation. Best-effort: skip silently
# if the helper script is missing or fails (the skill itself can install
# on first use).
if [ -x "$ROOT/scripts/setup-graphify-skill.sh" ]; then
  "$ROOT/scripts/setup-graphify-skill.sh" || \
    echo "→ graphify skill bootstrap skipped (rerun scripts/setup-graphify-skill.sh manually)."
fi

cat <<EOF

=== Setup complete ===
Next steps:
  • make start             # run the FastAPI server (then open http://localhost:8000)
  • make install-agents    # install the weekly-report launchd agent (daily ingestion is scheduled in-app)
  • make test              # run the test suite
EOF
