#!/usr/bin/env bash
# Ingest staged WhatsApp messages into the MCP server.
# Staging is populated in real-time by the whatsapp-rust Tokio task
# running inside the Tauri binary.
#
# Messages are grouped into conversation windows before ingestion so that
# short/emoji-only messages are not indexed without context.
set -euo pipefail


# ── log-line timestamps ────────────────────────────────────────
# Source the shared prefixer so every line of stdout+stderr from
# this point on carries an HH:MM:SS prefix in the connector log.
_ts_helper="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/../shared/log_timestamps.sh"
[ -r "$_ts_helper" ] && . "$_ts_helper" || true

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

# Resolve Python via the shared picker (bundled standalone > repo venv > PATH).
# This connector POSTs over HTTP, so pass the `httpx` probe so a candidate that
# can't import httpx is skipped rather than chosen and failing mid-run.
# shellcheck source=../shared/watch_common.sh disable=SC1091
. "$SCRIPT_DIR/../shared/watch_common.sh"
estormi_resolve_python httpx
estormi_mcp_url  # sets MCP_URL from the canonical shared default

# Known staging directories — checked in priority order.
# Each non-empty dir is ingested in sequence so no messages are missed.
_STAGING_CANDIDATES=(
  "$HOME/Library/Application Support/app.estormi.local/staging/whatsapp"
  "$HOME/estormi-staging/whatsapp"
)

_has_staged_files() {
  # Avoid ``ls *.meta.json`` here: an unmatched glob exits 1 and would
  # tear the script down under ``set -euo pipefail``. ``find`` always
  # returns 0 and prints nothing when there's no match, so the
  # ``-n`` test cleanly resolves to false on an empty staging dir.
  [ -d "$1" ] && [ -n "$(find "$1" -maxdepth 1 -name '*.meta.json' 2>/dev/null)" ]
}

# Trigger a bounded WhatsApp reconnect before ingesting staged files.
# The Tauri app exposes this endpoint even when the WhatsApp bot is not
# running continuously; wa.db keeps the pairing session for auto-reconnect.
if [ "${WHATSAPP_SYNC_ONCE:-true}" = "true" ]; then
  _sync_seconds="${WHATSAPP_SYNC_ONCE_SECONDS:-300}"
  # Forward the source's historic-depth window (in days) so the sidecar can
  # page older history on demand back to that horizon on a fresh pairing. Set by
  # apply_ingest_env_overrides from `whatsapp_historic_depth` (default 2y → 730).
  # Guard that it's purely numeric before splicing into the JSON body.
  _backfill_days="${WHATSAPP_HISTORY_DAYS:-}"
  if [ -n "$_backfill_days" ] && [ "$_backfill_days" -eq "$_backfill_days" ] 2>/dev/null; then
    _sync_body="{\"seconds\":${_sync_seconds},\"backfill_days\":${_backfill_days}}"
  else
    _sync_body="{\"seconds\":${_sync_seconds}}"
  fi
  echo "[whatsapp] Triggering bounded WhatsApp sync (cap ${_sync_seconds}s; exits early once paired + idle; backfill_days=${_backfill_days:-none})..."
  if command -v curl >/dev/null 2>&1; then
    _sync_started_at="$(date +%s)"
    # Pipe the auth header via stdin (`-H @-`) so the token never appears in
    # argv — otherwise any user on the box can read it via `ps`.
    printf 'X-Estormi-WA-Token: %s\n' "${ESTORMI_WA_TOKEN:-}" | curl -fsS \
      --max-time "$((_sync_seconds + 30))" \
      -X POST "http://127.0.0.1:9877/api/whatsapp/sync-once" \
      -H 'Content-Type: application/json' \
      -H @- \
      -d "$_sync_body" \
      >/dev/null \
      || echo "[whatsapp] Warning: bounded sync failed or Estormi WhatsApp API is unavailable."
    _sync_elapsed=$(($(date +%s) - _sync_started_at))
    echo "[whatsapp] Bounded sync step finished after ${_sync_elapsed}s."
  else
    echo "[whatsapp] Warning: curl not found; skipping bounded WhatsApp sync."
  fi
fi

echo "[whatsapp] Ingesting staged messages as conversation windows..."
_ingested_any=false
for _dir in "${_STAGING_CANDIDATES[@]}"; do
  if _has_staged_files "$_dir"; then
    echo "[whatsapp] Staging dir: $_dir"
    STAGING_DIR="$_dir" PYTHONPATH="$REPO_ROOT${PYTHONPATH:+:$PYTHONPATH}" \
      "$PY" -m estormi_ingestion.whatsapp.ingest_conversations
    _ingested_any=true
  fi
done
if [ "$_ingested_any" = false ]; then
  # An empty staging dir after sync-once has two distinct causes:
  #   (a) the bot is unpaired (or otherwise broken) and the sync window
  #       was a silent no-op — actionable: user must re-pair.
  #   (b) the bot is healthy but nothing new arrived during the window
  #       — non-actionable, expected steady state.
  # Pre-fix, both produced the same "No staged messages found" line,
  # which sounded like a path misconfiguration in case (b) and hid the
  # actionable hint in case (a). Query the bot's /status endpoint to
  # tell them apart and emit a clear, source-specific message.
  _wa_paired="unknown"
  if command -v curl >/dev/null 2>&1; then
    _status_body="$(
      printf 'X-Estormi-WA-Token: %s\n' "${ESTORMI_WA_TOKEN:-}" | curl -fsS \
        --max-time 5 \
        -X GET "http://127.0.0.1:9877/api/whatsapp/status" \
        -H @- 2>/dev/null || true
    )"
    case "$_status_body" in
      *'"paired":true'*)  _wa_paired="yes" ;;
      *'"paired":false'*) _wa_paired="no"  ;;
    esac
  fi
  case "$_wa_paired" in
    no)
      echo "[whatsapp] Bot is not paired — re-pair via Settings → WhatsApp." >&2
      ;;
    yes)
      echo "[whatsapp] Bot is paired; no new messages arrived during the ${_sync_seconds:-300}s sync window."
      ;;
    *)
      echo "[whatsapp] No staged messages and bot status unavailable — Estormi WhatsApp API may be down."
      ;;
  esac
fi
