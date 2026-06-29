#!/usr/bin/env bash
# Quick reachability check — MCP /health, Qdrant data dir, SQLite DB, LaunchAgents.
# For the full validation suite see scripts/test_suite.sh.
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
# shellcheck disable=SC1091
source .env 2>/dev/null || true

# Track failed checks so the script exits non-zero when any component is down
# — without this gate `make health` always returned 0 and CI / monitoring /
# the daily pipeline saw green even when MCP was offline and the DB was missing.
failed=0
status() { printf "  %-32s %s\n" "$1" "$2"; }
fail() {
  status "$1" "$2"
  failed=$((failed + 1))
}

echo "=== Estormi health ==="

if curl -sf "http://localhost:${MCP_SERVER_PORT:-8000}/health" >/dev/null; then
  status "MCP (:${MCP_SERVER_PORT:-8000})" "OK"
else
  fail "MCP (:${MCP_SERVER_PORT:-8000})" "FAIL"
fi

# Qdrant runs embedded in the MCP process; here we only confirm its on-disk
# data dir exists. Live reachability is already covered by the /health probe above.

DATA_DIR="${ESTORMI_DATA_DIR:-$HOME/Library/Application Support/Estormi}"
if [ -d "$DATA_DIR/qdrant" ]; then
  status "Qdrant data dir" "OK"
else
  fail "Qdrant data dir" "MISSING"
fi

if [ -f "$DATA_DIR/estormi.db" ]; then
  status "SQLite database" "OK"
else
  fail "SQLite database" "MISSING"
fi

agents=$(launchctl list 2>/dev/null | grep -c estormi || true)
# Count the plists actually shipped, so the denominator stays in sync if we
# add or drop scheduled agents.
total=$(ls "$ROOT"/scripts/app.estormi.local.*.plist 2>/dev/null | wc -l | tr -d ' ')
status "LaunchAgents loaded"           "${agents}/${total}"

exit "$failed"
