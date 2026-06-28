#!/usr/bin/env bash
# Run a companion prompt from prompts/companion/<slug>.md through the Claude CLI.
# The Claude CLI is configured with the Estormi MCP, so the prompt can
# call search_memory directly.
#
# Usage:
#   bash scripts/run_prompt.sh <slug> [var=value [var=value ...]]
#   bash scripts/run_prompt.sh project-context name=acme-migration
#
# Output: ~/estormi-reports/<date>-<slug>.md
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

if [ $# -lt 1 ]; then
  echo "usage: $0 <slug> [var=value ...]" >&2
  echo "available prompts:" >&2
  ls -1 "$ROOT/prompts/companion" 2>/dev/null | grep -E '\.md$' | sed 's/\.md$//' | sed 's/^/  /' >&2
  exit 2
fi

SLUG="$1"; shift
PROMPT_FILE="$ROOT/prompts/companion/$SLUG.md"

if [ ! -f "$PROMPT_FILE" ]; then
  echo "[run_prompt] no such prompt: $PROMPT_FILE" >&2
  exit 1
fi

# Load .env so any defaults defined there are available
# shellcheck disable=SC1091
source "$ROOT/.env" 2>/dev/null || true

# Auto-fill standard date variables if not provided.
TODAY=$(date +%Y-%m-%d)
WEEK_AGO=$(date -v-7d +%Y-%m-%d 2>/dev/null || date -d '7 days ago' +%Y-%m-%d)
WEEK_LABEL="${WEEK_AGO} → ${TODAY}"

# Default var pairs as KEY=VALUE; caller can override.
declare -a KV_PAIRS=(
  "after=$WEEK_AGO"
  "before=$TODAY"
  "today=$TODAY"
  "week_label=$WEEK_LABEL"
)
for kv in "$@"; do
  if [[ "$kv" == *=* ]]; then
    KV_PAIRS+=("$kv")
  fi
done

# Substitute {{var}} placeholders via Python (safe for any value).
PROMPT_BODY=$(
  python3 - "$PROMPT_FILE" "${KV_PAIRS[@]}" <<'PY'
import sys, pathlib
body = pathlib.Path(sys.argv[1]).read_text()
for kv in sys.argv[2:]:
    if "=" not in kv:
        continue
    k, _, v = kv.partition("=")
    body = body.replace("{{" + k + "}}", v)
sys.stdout.write(body)
PY
)

REPORTS_DIR="${REPORTS_DIR:-$HOME/estormi-reports}"
mkdir -p "$REPORTS_DIR"
OUT_FILE="$REPORTS_DIR/${TODAY}-${SLUG}.md"

CLAUDE_BIN="${CLAUDE_BIN:-/opt/homebrew/bin/claude}"
if ! command -v "$CLAUDE_BIN" &>/dev/null; then
  echo "[run_prompt] claude CLI not found at $CLAUDE_BIN" >&2
  exit 1
fi

echo "[run_prompt] $SLUG → $OUT_FILE"

REPORT=$("$CLAUDE_BIN" --print "$PROMPT_BODY" 2>/dev/null) || {
  echo "[run_prompt] claude CLI failed" >&2
  exit 1
}

printf "%s\n" "$REPORT" > "$OUT_FILE"
echo "[run_prompt] saved $(wc -c <"$OUT_FILE") bytes"

if command -v osascript &>/dev/null; then
  osascript -e "display notification \"$SLUG → $OUT_FILE\" with title \"Estormi — prompt\"" 2>/dev/null || true
fi
