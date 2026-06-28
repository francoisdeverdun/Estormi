#!/usr/bin/env bash
# Weekly freshness report — runs freshness_check.py, asks Claude to write a
# narrative summary, and saves the result to ~/estormi-reports/.
# Triggered by the app.estormi.local.weekly-report launchd agent every
# Sunday at 20:00, but can also be run manually:
#   bash scripts/weekly_report.sh
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
# shellcheck disable=SC1091
source .env 2>/dev/null || true

REPORTS_DIR="${REPORTS_DIR:-$HOME/estormi-reports}"
mkdir -p "$REPORTS_DIR"

DATE=$(date +%Y-%m-%d)
OUT_FILE="$REPORTS_DIR/$DATE.md"

CLAUDE_BIN="${CLAUDE_BIN:-/opt/homebrew/bin/claude}"
PYTHON="${PYTHON:-$ROOT/.venv/bin/python3}"
# Fall back to system python3 if the venv isn't set up
[ -x "$PYTHON" ] || PYTHON=python3

echo "[weekly-report] Collecting freshness data…"
FRESHNESS_JSON=$("$PYTHON" "$ROOT/scripts/freshness_check.py" --json 2>/dev/null) || {
  echo "[weekly-report] freshness_check.py failed" >&2
  exit 1
}

SERVICES_STATUS=$(bash "$ROOT/scripts/health_check.sh" 2>/dev/null || echo "(service check failed)")

PROMPT="You are the user's personal assistant. Below are the weekly metrics of their personal second brain (a semantic memory system indexing their notes, emails, iMessages, WhatsApp, calendar, code, etc.).

## Freshness data (JSON)
\`\`\`json
$FRESHNESS_JSON
\`\`\`

## Service status
\`\`\`
$SERVICES_STATUS
\`\`\`

Write a weekly freshness report in markdown (H1 title with the date, max 400 words). Structure:
1. **One-sentence summary**: all is well, or there are problems.
2. **Sources to watch**: list only stale sources or those with 0 chunks in the last 24h, with the observed vs expected delay.
3. **Key figures for the week**: total chunks indexed this week per source (top 5 only).
4. **launchd agents**: list only those in error (exit code != 0).
5. **Recommended actions**: actionable bullets if problems are detected, otherwise 'Nothing to do'.

Be factual and concise, in English."

echo "[weekly-report] Asking Claude to generate report…"
if command -v "$CLAUDE_BIN" &>/dev/null; then
  REPORT=$("$CLAUDE_BIN" --print "$PROMPT" 2>/dev/null) || {
    echo "[weekly-report] claude CLI failed" >&2
    # Write raw JSON as fallback
    printf "# Estormi Report %s\n\n(Claude generation failed)\n\n\`\`\`json\n%s\n\`\`\`\n" "$DATE" "$FRESHNESS_JSON" > "$OUT_FILE"
    exit 1
  }
else
  echo "[weekly-report] claude CLI not found at $CLAUDE_BIN" >&2
  exit 1
fi

printf "%s\n" "$REPORT" > "$OUT_FILE"
echo "[weekly-report] Report saved: $OUT_FILE"

# macOS notification
if command -v osascript &>/dev/null; then
  STALE=$(echo "$FRESHNESS_JSON" | "$PYTHON" -c "
import json,sys
d=json.load(sys.stdin)
n=d['summary']['stale_sources']
t=d['summary']['total_sources']
print(f'{n}/{t} sources stale' if n else 'All sources are fresh')
" 2>/dev/null || echo "see report")
  osascript -e "display notification \"$STALE — report in ~/estormi-reports/$DATE.md\" with title \"Estormi — weekly report\"" 2>/dev/null || true
fi
