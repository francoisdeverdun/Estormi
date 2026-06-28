#!/usr/bin/env bash
# Shared helpers sourced by the per-source watch/ingest scripts
# (apple_notes, apple_mail, imessage, reminders, whatsapp).
#
# Only the two pieces that were byte-identical across those scripts live
# here: the Python-binary picker and the watermark-window computation. The
# per-record ingest logic lives in each source's own importable module
# (`estormi_ingestion.<source>.ingest`, invoked via `python -m`) — it diverges
# too much (different payload schemas, chunking vs single-shot, HTML stripping,
# group-type maps, OTP policy) to share safely.
#
# Callers must set REPO_ROOT before sourcing-and-calling.

# Canonical loopback default for the MCP server URL — the single source of
# truth shared with the Python side (estormi_ingestion/shared/config.py).
# Keep these two in lockstep: the literal IP (not "localhost") avoids a DNS
# round-trip and any /etc/hosts skew, and matches the server's bind address.
ESTORMI_DEFAULT_MCP_URL="http://127.0.0.1:8000"

# Resolve the MCP server base URL into the MCP_URL variable: the MCP_SERVER_URL
# env override when set, else the canonical default above. Use this instead of
# inlining a per-script default so the default can never drift.
estormi_mcp_url() {
  MCP_URL="${MCP_SERVER_URL:-$ESTORMI_DEFAULT_MCP_URL}"
}

# Resolve the Python interpreter into the PY variable (no-op if PY is
# already set, e.g. by the app or a test). Precedence: bundled standalone
# (`python/` inside the app) > repo venv > system PATH. The bundled Python
# is preferred so the app can ingest without a healthy machine Python.
#
# The bundled interpreter and the dev venv both live at the *repo root* —
# one level above the `packages/` import root callers put in REPO_ROOT. We
# derive that root from this helper's own location (it sits at
# `packages/estormi_ingestion/shared/`, so the root is three levels up)
# rather than from the caller's REPO_ROOT: after the move of the Python
# packages under `packages/`, REPO_ROOT points at `packages/`, where there
# is no `python/` or `.venv/`, and reusing it here silently fell through to
# the system Python (which lacks memory_core / pyobjc).
#
# Optional first arg: a module name to import-probe each candidate with (e.g.
# `httpx`). When set, a candidate is accepted only if it both exists and can
# import that module — the whatsapp connector POSTs over HTTP and needs that
# guarantee. Without it, candidates are accepted on existence alone.
estormi_resolve_python() {
  local probe_mod="${1:-}"
  [ -n "${PY:-}" ] && return 0
  local _root
  _root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
  local candidate
  for candidate in \
      "$_root/python/bin/python3" \
      "$_root/.venv/bin/python3" \
      "$(command -v python3 2>/dev/null)"; do
    [ -x "$candidate" ] || continue
    if [ -n "$probe_mod" ] && ! "$candidate" -c "import $probe_mod" 2>/dev/null; then
      continue
    fi
    PY="$candidate"
    return 0
  done
  # No candidate matched (or none could import the probe module); fall back to
  # whatever `python3` resolves to so callers still have something to run.
  PY="${PY:-python3}"
}

# Echo the lookback window in days for a source.
#
#   estormi_watermark_window <source_key> <first_run_days> <error_fallback_days>
#
# On a normal run the window is derived from the stored watermark
# (days since the watermark, +1, floor 1). On first init (no watermark)
# it falls back to <first_run_days> — typically the historic-depth value
# the app sets from the Manage modal picker, or 90. If the days-since
# computation itself fails it falls back to <error_fallback_days>.
#
# Shell values are passed as argv (sys.argv), never interpolated into the
# code string, so a repo path containing quotes/apostrophes is safe.
estormi_watermark_window() {
  local source_key="$1"
  local first_run_days="$2"
  local error_fallback_days="$3"
  local since days

  since=$("$PY" -c '
import asyncio, sys
sys.path.insert(0, sys.argv[1])
from estormi_ingestion.shared.watermark import get_watermark
ts, _ = asyncio.run(get_watermark(sys.argv[2]))
print(ts or "")
' "$REPO_ROOT" "$source_key" 2>/dev/null || echo "")

  if [ -n "$since" ]; then
    days=$("$PY" -c '
import sys
from datetime import datetime, timezone
since = datetime.fromisoformat(sys.argv[1])
if since.tzinfo is None:
    since = since.replace(tzinfo=timezone.utc)
print(max(1, (datetime.now(timezone.utc) - since).days + 1))
' "$since" 2>/dev/null || echo "$error_fallback_days")
  else
    days="$first_run_days"
  fi
  printf '%s\n' "$days"
}
