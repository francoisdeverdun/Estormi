#!/usr/bin/env bash
# Export recent Apple Mail messages and POST each chunk to the MCP server.
set -euo pipefail


# ── log-line timestamps ────────────────────────────────────────
# Source the shared prefixer so every line of stdout+stderr from
# this point on carries an HH:MM:SS prefix in the connector log.
_ts_helper="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/../shared/log_timestamps.sh"
[ -r "$_ts_helper" ] && . "$_ts_helper" || true

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
STAGING="${STAGING_DIR:-$HOME/estormi-staging/mail}"
CHUNK_SIZE="${MAIL_CHUNK_SIZE:-1000}"
CHUNK_OVERLAP="${MAIL_CHUNK_OVERLAP:-150}"

# shellcheck source=../shared/watch_common.sh disable=SC1091
. "$SCRIPT_DIR/../shared/watch_common.sh"
estormi_resolve_python
estormi_mcp_url  # sets MCP_URL from the canonical shared default

# Read watermark.
# First init (no watermark): backfill the historic-depth window
# (MAIL_DAYS_WINDOW — set by the app from the Manage modal picker), or
# 90 days when nothing has been chosen.
# Subsequent runs: window derived from watermark.
DAYS_WINDOW="$(estormi_watermark_window "mail" "${MAIL_DAYS_WINDOW:-90}" 7)"
TIMEOUT_SECS=18000

mkdir -p "$STAGING"

# Capture start time BEFORE the export starts — see apple_notes/watch_and_ingest.sh.
START_TS="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

echo "[mail] Step 1/3 — Exporting via AppleScript (window: ${DAYS_WINDOW}d, timeout: ${TIMEOUT_SECS}s)..."
PYTHONPATH="$REPO_ROOT${PYTHONPATH:+:$PYTHONPATH}" \
"$PY" -m estormi_ingestion.shared.host.app_lifecycle --app Mail -- \
    osascript "$SCRIPT_DIR/export_mail.applescript" "$DAYS_WINDOW" "$TIMEOUT_SECS" "$STAGING" &
_OSAS_PID=$!
_EXPORT_START=$(date +%s)
while kill -0 "$_OSAS_PID" 2>/dev/null; do
    sleep 30
    _ELAPSED=$(( $(date +%s) - _EXPORT_START ))
    _STAGED=$(find "$STAGING" -maxdepth 1 -name "*.txt" 2>/dev/null | wc -l | tr -d ' ')
    printf "[mail] Still exporting... %ds elapsed, %s messages staged so far\n" "$_ELAPSED" "$_STAGED"
done
if wait "$_OSAS_PID"; then
    _EXPORT_RC=0
else
    _EXPORT_RC=$?
    echo "[mail] WARNING: AppleScript exited with error (${_EXPORT_RC}) — processing files staged so far; watermark will NOT advance so the window re-pulls next run"
fi

_TOTAL=$( find "$STAGING" -maxdepth 1 -name "*.meta.json" 2>/dev/null| wc -l | tr -d ' ')
echo "[mail] Step 2/3 — Export complete: ${_TOTAL} messages ready to ingest."
echo "[mail] Step 3/3 — Chunking + posting to MCP server..."
shopt -s nullglob
count=0
failed=0
for meta_file in "$STAGING"/*.meta.json; do
  msg_id="$(basename "$meta_file" .meta.json)"
  body_file="$STAGING/$msg_id.txt"
  [ -f "$body_file" ] || continue
  count=$((count + 1))
  TITLE=$("$PY" -c "import json,sys;m=json.load(open(sys.argv[1]));print((m.get('title') or m.get('id') or '?')[:60])" "$meta_file" 2>/dev/null || echo '?')
  printf "[mail]  · %d/%s — %s\n" "$count" "$_TOTAL" "$TITLE"

  # Capture the ingest exit code explicitly so a single failed POST
  # (raise_for_status) keeps the staged files and counts it rather than
  # tearing the whole loop down under `set -e` (mirrors notes/imessage).
  # The per-message chunk+POST logic (and thread_root_key) lives in an
  # importable module (estormi_ingestion.apple_mail.ingest) so it is
  # unit-testable — a heredoc body is never executed by the test suite, which is
  # how a post_chunks TypeError once shipped. REPO_ROOT (= packages/) on
  # PYTHONPATH so `-m` resolves in both the dev checkout and the bundled app;
  # the positional argv order is preserved.
  set +e
  PYTHONPATH="$REPO_ROOT${PYTHONPATH:+:$PYTHONPATH}" \
    "$PY" -m estormi_ingestion.apple_mail.ingest \
    "$meta_file" "$body_file" "$MCP_URL" "$REPO_ROOT" "$CHUNK_SIZE" "$CHUNK_OVERLAP"
  _ingest_rc=$?
  set -e
  if [ "$_ingest_rc" -ne 0 ]; then
    failed=$((failed + 1))
    echo "[mail] WARNING: ingest failed for $msg_id — keeping staged files for next run" >&2
    continue
  fi

  rm -f "$body_file" "$meta_file"
done

echo "[mail] Done — ${count} messages processed (${failed} failed)."

# Write watermark only if the export completed cleanly AND every message
# ingested — START_TS captured before the export began so a message modified
# mid-export is still picked up next time. A partial export/ingest must leave
# the previous watermark in place so the next run re-pulls the window (mirrors
# imessage/watch_and_ingest.sh).
if [ "$_EXPORT_RC" -eq 0 ] && [ "$failed" -eq 0 ]; then
  "$PY" -c '
import asyncio, sys
sys.path.insert(0, sys.argv[1])
from estormi_ingestion.shared.watermark import set_watermark
asyncio.run(set_watermark("mail", sys.argv[2]))
' "$REPO_ROOT" "$START_TS"
else
  echo "[mail] Watermark NOT advanced — export incomplete or ${failed} message(s) failed to ingest; next run re-pulls the ${DAYS_WINDOW}d window." >&2
fi
