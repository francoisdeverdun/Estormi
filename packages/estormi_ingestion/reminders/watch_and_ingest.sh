#!/usr/bin/env bash
# Export all Reminders and POST each chunk to the MCP server.
# Full dump every run — dedup is handled server-side via content_hash.
set -euo pipefail


# ── log-line timestamps ────────────────────────────────────────
# Source the shared prefixer so every line of stdout+stderr from
# this point on carries an HH:MM:SS prefix in the connector log.
_ts_helper="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/../shared/log_timestamps.sh"
[ -r "$_ts_helper" ] && . "$_ts_helper" || true

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
STAGING="${STAGING_DIR:-$HOME/estormi-staging/reminders}"

# shellcheck source=../shared/watch_common.sh disable=SC1091
. "$SCRIPT_DIR/../shared/watch_common.sh"
estormi_resolve_python
estormi_mcp_url  # sets MCP_URL from the canonical shared default

mkdir -p "$STAGING"

# Capture start time BEFORE listing reminders — see apple_notes/watch_and_ingest.sh.
START_TS="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

# Export via the bundled Python (PyObjC EventKit). Using $PY — the same
# interpreter the activation permission probe runs in — means the reading
# process shares the TCC client identity the Reminders grant was attributed
# to, so the user is not re-prompted mid-run. (A standalone swiftc binary,
# whose code identity differed and changed per compile, never inherited it.)
echo "[reminders] Step 1/3 — Exporting via EventKit (Python)..."
"$PY" "$SCRIPT_DIR/export_reminders.py"

_TOTAL=$( find "$STAGING" -maxdepth 1 -name "*.meta.json" 2>/dev/null| wc -l | tr -d ' ')
echo "[reminders] Step 2/3 — Export complete: ${_TOTAL} reminders ready to ingest."
echo "[reminders] Step 3/3 — Posting reminders to MCP server..."
shopt -s nullglob

# Collect all IDs that are currently pending (before processing, so we can
# prune completed reminders from the DB afterward).
EXPORTED_IDS=()
for meta_file in "$STAGING"/*.meta.json; do
  EXPORTED_IDS+=("$(basename "$meta_file" .meta.json)")
done

count=0
failed=0
for meta_file in "$STAGING"/*.meta.json; do
  rem_id="$(basename "$meta_file" .meta.json)"
  body_file="$STAGING/$rem_id.txt"
  [ -f "$body_file" ] || continue
  count=$((count + 1))
  TITLE=$("$PY" -c "import json,sys;m=json.load(open(sys.argv[1]));print((m.get('title') or m.get('id') or '?')[:60])" "$meta_file" 2>/dev/null || echo '?')
  printf "[reminders]  · %d/%s — %s\n" "$count" "$_TOTAL" "$TITLE"

  # Isolate per-reminder failures: a single transient POST error must not tear
  # the whole stage down under `set -e` (mirrors calendar/notes/imessage). On
  # failure, keep the staged files so the next run retries.
  # The per-reminder POST logic lives in an importable module
  # (estormi_ingestion.reminders.ingest) so it is unit-testable — a heredoc body
  # is never executed by the test suite, which is how a post_chunks TypeError
  # once shipped. REPO_ROOT (= packages/) on PYTHONPATH so `-m` resolves in both
  # the dev checkout and the bundled app; the positional argv order is preserved.
  set +e
  PYTHONPATH="$REPO_ROOT${PYTHONPATH:+:$PYTHONPATH}" \
    "$PY" -m estormi_ingestion.reminders.ingest \
    "$meta_file" "$body_file" "$MCP_URL" "$REPO_ROOT"
  _ingest_rc=$?
  set -e
  if [ "$_ingest_rc" -ne 0 ]; then
    failed=$((failed + 1))
    echo "[reminders] WARN: ingest failed for ${rem_id}; keeping staged files for retry." >&2
    continue
  fi

  rm -f "$body_file" "$meta_file"
done

echo "[reminders] Done — ${count} reminders processed (${failed} failed)."

# Mark completed reminders in DB (those no longer in the pending export).
# We keep the chunks for historical search but flag them so the daily note
# no longer surfaces them as overdue.
#
# CRITICAL: this UPDATE deletes (logically) every reminder that isn't in
# EXPORTED_IDS. If the reminders exporter partial-failed — e.g. a transient
# EventKit error or a per-reminder write failure — EXPORTED_IDS is missing
# those reminders and we would wrongly mark them completed=1, hiding them
# from the daily briefing forever. The exporter writes _export_complete.flag
# only after EVERY reminder was persisted; absent the flag, we skip the
# destructive UPDATE and surface a warning instead.
_FLAG="$STAGING/_export_complete.flag"
if [ ! -f "$_FLAG" ]; then
  echo "[reminders] WARN: _export_complete.flag missing — reminders exporter did not enumerate every reminder. Skipping mark-complete to avoid losing live reminders." >&2
elif [ "${#EXPORTED_IDS[@]}" -gt 0 ]; then
  EXPORTED_JSON="$(printf '%s\n' "${EXPORTED_IDS[@]}" | "$PY" -c "import sys,json; print(json.dumps(sys.stdin.read().splitlines()))")"
  # Honour ESTORMI_DB / ESTORMI_DATA_DIR overrides — every other ingestion
  # path does, this one used to hard-code ~/Library/Application Support/Estormi.
  ESTORMI_DB_PATH="${ESTORMI_DB:-${ESTORMI_DATA_DIR:-$HOME/Library/Application Support/Estormi}/estormi.db}"
  # The mark-complete DB update lives in an importable module
  # (estormi_ingestion.reminders.mark_complete) so it is unit-testable. It
  # preserves the STDOUT contract (prints "[reminders] Marked N …" only when at
  # least one reminder was newly completed). REPO_ROOT (= packages/) on
  # PYTHONPATH so `-m` resolves in both the dev checkout and the bundled app;
  # the positional argv order is preserved.
  PYTHONPATH="$REPO_ROOT${PYTHONPATH:+:$PYTHONPATH}" \
    "$PY" -m estormi_ingestion.reminders.mark_complete "$EXPORTED_JSON" "$ESTORMI_DB_PATH"
fi

# Drop the completeness flag now that we're done with it. The reminders exporter
# recreates it from scratch on the next run after a clean enumeration; a
# stale flag surviving into a partial-failure run would re-enable the
# data-loss path this guard exists to prevent.
rm -f "$_FLAG"

# Write watermark only on a fully clean run — use the start-of-run timestamp
# so a record modified during the export is still picked up next time. If any
# reminder failed to ingest, leave the previous watermark in place so the next
# run retries instead of being treated as fully caught up.
if [ "$failed" -eq 0 ]; then
  "$PY" -c '
import asyncio, sys
sys.path.insert(0, sys.argv[1])
from estormi_ingestion.shared.watermark import set_watermark
asyncio.run(set_watermark("reminders", sys.argv[2]))
' "$REPO_ROOT" "$START_TS"
else
  echo "[reminders] WARN: ${failed} reminder(s) failed — watermark not advanced." >&2
fi
