#!/usr/bin/env bash
# Export recent iMessages and POST each chunk to the MCP server.
set -euo pipefail


# ── log-line timestamps ────────────────────────────────────────
# Source the shared prefixer so every line of stdout+stderr from
# this point on carries an HH:MM:SS prefix in the connector log.
_ts_helper="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/../shared/log_timestamps.sh"
[ -r "$_ts_helper" ] && . "$_ts_helper" || true

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
STAGING="${STAGING_DIR:-$HOME/estormi-staging/imessage}"
CHUNK_SIZE="${IMESSAGE_CHUNK_SIZE:-800}"
CHUNK_OVERLAP="${IMESSAGE_CHUNK_OVERLAP:-100}"

# shellcheck source=../shared/watch_common.sh disable=SC1091
. "$SCRIPT_DIR/../shared/watch_common.sh"
estormi_resolve_python
estormi_mcp_url  # sets MCP_URL from the canonical shared default

# shellcheck disable=SC1091
[ -f "$REPO_ROOT/.env" ] && source "$REPO_ROOT/.env"

# Read watermark; on first run fall back to the historic-depth window
# (IMESSAGE_DAYS_WINDOW — set by the app from the Manage modal picker),
# or 90 days when nothing has been chosen. Never default to "everything".
DAYS="$(estormi_watermark_window "imessage" "${IMESSAGE_DAYS_WINDOW:-90}" 7)"

mkdir -p "$STAGING"

# Capture start time BEFORE fetching — see apple_notes/watch_and_ingest.sh. We
# prefer max(message.date) once the fetcher runs (it writes the value into
# $STAGING/_max_date.txt), but fall back to START_TS if it is unavailable.
START_TS="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
rm -f "$STAGING/_max_date.txt"

echo "[imessage] Step 1/3 — Fetching last ${DAYS}d of messages..."
# fetch_imessages.py exits 2 when chat.db is unreadable due to a missing
# Full Disk Access grant. Catch that specific code so the wrapper can
# stop cleanly with a one-line setup hint instead of letting `set -e`
# turn it into a generic "exit 1 — FAILED in 0.1s" line that buries the
# actionable message above it.
set +e
STAGING_DIR="$STAGING" IMESSAGE_DAYS_WINDOW="$DAYS" \
  PYTHONPATH="$REPO_ROOT${PYTHONPATH:+:$PYTHONPATH}" \
  "$PY" -m estormi_ingestion.imessage.fetch_imessages
_fetch_rc=$?
set -e
if [ "$_fetch_rc" -eq 2 ]; then
  echo "[imessage] Stopping run — Full Disk Access required." >&2
  exit 2
fi
if [ "$_fetch_rc" -ne 0 ]; then
  exit "$_fetch_rc"
fi

_TOTAL=$( find "$STAGING" -maxdepth 1 -name "*.meta.json" 2>/dev/null| wc -l | tr -d ' ')
echo "[imessage] Step 2/3 — Fetch complete: ${_TOTAL} messages ready to ingest."
echo "[imessage] Step 3/3 — Posting messages to MCP server..."
shopt -s nullglob
count=0
failed=0
for meta_file in "$STAGING"/*.meta.json; do
  msg_id="$(basename "$meta_file" .meta.json)"
  body_file="$STAGING/$msg_id.txt"
  [ -f "$body_file" ] || continue
  count=$((count + 1))
  TITLE=$("$PY" -c "import json,sys;m=json.load(open(sys.argv[1]));print((m.get('chat_name') or m.get('chat_id') or m.get('id') or '?')[:60])" "$meta_file" 2>/dev/null || echo '?')
  if (( count % 25 == 1 )); then
    printf "[imessage]  · %d/%s — %s\n" "$count" "$_TOTAL" "$TITLE"
  fi

  # The per-message chunk+POST logic lives in an importable module
  # (estormi_ingestion.imessage.ingest) so it is unit-testable — a heredoc body
  # is never executed by the test suite, which is how a post_chunks TypeError
  # once shipped. REPO_ROOT (= packages/) on PYTHONPATH so `-m` resolves in both
  # the dev checkout and the bundled app; the positional argv order is preserved.
  if ! PYTHONPATH="$REPO_ROOT${PYTHONPATH:+:$PYTHONPATH}" \
      "$PY" -m estormi_ingestion.imessage.ingest \
      "$meta_file" "$body_file" "$MCP_URL" "$REPO_ROOT" "$CHUNK_SIZE" "$CHUNK_OVERLAP"
  then
    failed=$((failed + 1))
    echo "[imessage] WARNING: ingest failed for $msg_id — keeping staged files for next run" >&2
    continue
  fi

  rm -f "$body_file" "$meta_file"
done

echo "[imessage] Done — ${count} messages processed (${failed} failed)."

# Write watermark only if every message ingested cleanly. A partial failure
# must leave the previous watermark in place so the next run retries. Prefer
# max(message.date) — the highest timestamp the fetcher actually observed —
# so the watermark lands on a message boundary; fall back to START_TS taken
# before the fetch began.
if [ "$failed" -eq 0 ]; then
  WM_TS="$START_TS"
  if [ -s "$STAGING/_max_date.txt" ]; then
    WM_TS="$(cat "$STAGING/_max_date.txt")"
  fi
  "$PY" -c '
import asyncio, sys
sys.path.insert(0, sys.argv[1])
from estormi_ingestion.shared.watermark import set_watermark
asyncio.run(set_watermark("imessage", sys.argv[2]))
' "$REPO_ROOT" "$WM_TS"
  rm -f "$STAGING/_max_date.txt"
else
  echo "[imessage] Watermark NOT advanced — ${failed} message(s) failed to ingest." >&2
fi
