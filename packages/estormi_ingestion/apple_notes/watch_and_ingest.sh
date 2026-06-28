#!/usr/bin/env bash
# Export recent Apple Notes and POST each chunk to the MCP server.
set -euo pipefail


# ── log-line timestamps ────────────────────────────────────────
# Source the shared prefixer so every line of stdout+stderr from
# this point on carries an HH:MM:SS prefix in the connector log.
_ts_helper="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/../shared/log_timestamps.sh"
[ -r "$_ts_helper" ] && . "$_ts_helper" || true

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
STAGING="${STAGING_DIR:-$HOME/estormi-staging/notes}"
CHUNK_SIZE="${NOTES_CHUNK_SIZE:-900}"

# shellcheck source=../shared/watch_common.sh disable=SC1091
. "$SCRIPT_DIR/../shared/watch_common.sh"
estormi_resolve_python
estormi_mcp_url  # sets MCP_URL from the canonical shared default

# Read watermark. First run (no watermark) backfills NOTES_DAYS_WINDOW (the
# Manage modal picker), or 90 days. A failed days-since computation falls
# back to a wide 365-day window so notes are never silently skipped.
DAYS_WINDOW="$(estormi_watermark_window "notes" "${NOTES_DAYS_WINDOW:-90}" 365)"

# AppleScript export timeout. Without an explicit value the script would
# fall back to its 1h default; a large library can need longer, so allow
# an override and pass it through (mirrors the mail/calendar exporters).
TIMEOUT_SECS="${NOTES_TIMEOUT_SECS:-18000}"

mkdir -p "$STAGING"

# Capture start time BEFORE listing notes — writing `date -u` after the
# run would let concurrent edits during the export drift past the watermark.
START_TS="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

echo "[notes] Step 1/3 — Exporting via AppleScript (window: ${DAYS_WINDOW}d, timeout: ${TIMEOUT_SECS}s)..."
echo "[notes]   AppleScript runs in-process; progress is silent until it finishes."

# Background-launch osascript so we can poll the staging dir and emit
# heartbeats. Without this the log freezes at "Exporting via AppleScript…"
# for minutes on a large library and the user has no signal that anything
# is happening.
PYTHONPATH="$REPO_ROOT${PYTHONPATH:+:$PYTHONPATH}" \
"$PY" -m estormi_ingestion.shared.host.app_lifecycle --app Notes -- \
    osascript "$SCRIPT_DIR/export_notes.applescript" "$DAYS_WINDOW" "$TIMEOUT_SECS" "$STAGING" &
_OSAS_PID=$!
_STARTED=$(date +%s)
while kill -0 "$_OSAS_PID" 2>/dev/null; do
  sleep 5
  _ELAPSED=$(( $(date +%s) - _STARTED ))
  _STAGED=$(find "$STAGING" -maxdepth 1 -name '*.meta.json' 2>/dev/null | wc -l | tr -d ' ')
  printf "[notes]   Still exporting... %ds elapsed, %s notes staged so far\n" \
    "$_ELAPSED" "$_STAGED"
done
# Capture the export exit code explicitly (mirrors apple_mail). A non-zero
# export — e.g. the AppleScript `with timeout` firing on a large library —
# stages only a subset of notes; the loop below counts only *staged*-then-
# failed ingests, so un-exported notes would otherwise be invisible and the
# watermark would advance past them, dropping them permanently. Gate the
# watermark on a clean export too.
if wait "$_OSAS_PID"; then
  _EXPORT_RC=0
else
  _EXPORT_RC=$?
  echo "[notes]   WARNING: AppleScript exited with error (${_EXPORT_RC}) — processing whatever was staged; watermark will NOT advance so the window re-pulls next run"
fi

_TOTAL=$(find "$STAGING" -maxdepth 1 -name '*.meta.json' 2>/dev/null | wc -l | tr -d ' ')
echo "[notes] Step 2/3 — Export complete: ${_TOTAL} notes ready to ingest."

echo "[notes] Step 3/3 — Chunking + posting to MCP server..."
shopt -s nullglob
count=0
chunk_total=0
failed=0
for meta_file in "$STAGING"/*.meta.json; do
  note_id="$(basename "$meta_file" .meta.json)"
  html_file="$STAGING/$note_id.html"
  [ -f "$html_file" ] || continue
  count=$((count + 1))

  # Print a per-note header so the log shows real progress through the
  # staging set (e.g. "[notes]  · 12/431 — Project ideas").
  TITLE=$("$PY" -c "import json,sys;m=json.load(open(sys.argv[1]));print((m.get('title') or m.get('id') or '?')[:60])" "$meta_file" 2>/dev/null || echo '?')
  printf "[notes]  · %d/%s — %s\n" "$count" "$_TOTAL" "$TITLE"

  # Capture the ingest exit code explicitly. A command-substitution
  # assignment masks the inner failure from `set -e`, so without this gate
  # a failed POST (raise_for_status) would still fall through to the rm
  # below and advance the watermark — silently dropping the note. Mirror
  # the imessage/documents/code/gcal scripts: on failure, keep the staged
  # files and count it so the watermark is held back.
  # The per-note chunk+POST logic lives in an importable module
  # (estormi_ingestion.apple_notes.ingest) so it is unit-testable — a heredoc
  # body is never executed by the test suite, which is how a post_chunks
  # TypeError once shipped. The module preserves the STDOUT contract exactly: it
  # prints the chunk count (or 0 on an empty/OTP note) on stdout, captured here
  # into CHUNKS_FOR_NOTE. REPO_ROOT (= packages/) on PYTHONPATH so `-m` resolves
  # in both the dev checkout and the bundled app; the positional argv order is
  # preserved.
  set +e
  CHUNKS_FOR_NOTE=$(PYTHONPATH="$REPO_ROOT${PYTHONPATH:+:$PYTHONPATH}" \
    "$PY" -m estormi_ingestion.apple_notes.ingest \
    "$meta_file" "$html_file" "$MCP_URL" "$REPO_ROOT" "$CHUNK_SIZE")
  _ingest_rc=$?
  set -e
  if [ "$_ingest_rc" -ne 0 ]; then
    failed=$((failed + 1))
    echo "[notes] WARNING: ingest failed for $note_id — keeping staged files for next run" >&2
    continue
  fi
  chunk_total=$((chunk_total + ${CHUNKS_FOR_NOTE:-0}))

  rm -f "$html_file" "$meta_file"
done

echo "[notes] Done — ${count} notes processed, ${chunk_total} chunks indexed (${failed} failed)."

# Write watermark only if the export completed cleanly AND every note ingested.
# A partial export OR a partial ingest must leave the previous watermark in
# place so the next run re-pulls the window. START_TS was captured before
# listing so concurrent edits don't drift past us.
if [ "$_EXPORT_RC" -eq 0 ] && [ "$failed" -eq 0 ]; then
  "$PY" -c '
import asyncio, sys
sys.path.insert(0, sys.argv[1])
from estormi_ingestion.shared.watermark import set_watermark
asyncio.run(set_watermark("notes", sys.argv[2]))
' "$REPO_ROOT" "$START_TS"
else
  echo "[notes] Watermark NOT advanced — export incomplete or ${failed} note(s) failed to ingest; next run re-pulls the ${DAYS_WINDOW}d window." >&2
fi
