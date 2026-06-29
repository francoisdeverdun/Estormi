#!/usr/bin/env bash
# Daily ingestion pipeline. Runs the ingestion stages with bounded parallelism.
#
# The stages are independent — no stage reads another's output, and all of the
# heavy work (embedding) happens server-side behind a single lock — so several
# source processes can run at once and only their cheap fetch/parse/POST phases
# overlap. Wall-time drops from the sum of all stage durations toward the
# longest single stage. Concurrency is capped (see ESTORMI_INGEST_PARALLELISM).
#
# (The dag_runs/dag_stages tables, the dag_state CLI and the `[dag]`/`dag.*`
# log tokens keep their legacy names: they are a persisted/log-parser contract
# shared with the iOS app, the web UI and server/jobs.py.)
#
# Usage:
#   bash scripts/daily_ingestion.sh                          # run all stages
#   STAGES="notes mail" bash scripts/daily_ingestion.sh      # subset
#   ESTORMI_INGEST_PARALLELISM=1 bash scripts/daily_ingestion.sh  # force serial
#
# Triggered by: the app's in-process APScheduler (default 02:00 daily) and a
# startup catch-up (estormi_server/server/lifespan.py), the sources panel, or
# `make daily-dag`. Running under the app keeps macOS permission grants
# attributed to Estormi — see estormi_server/server/permission_preflight.py.
set -uo pipefail

# ── single-step log rotation ───────────────────────────────────
# `tee -a` would otherwise grow estormi-daily-dag.log unbounded. Rotate
# once at the top of the run when it crosses 10 MB; we keep exactly one
# previous file (`.log.1`) — debugging never needs more than that, and a
# real log shipper isn't worth the dependency on a personal Mac.
_rotate_log="${ESTORMI_DATA_DIR:-$HOME/Library/Application Support/Estormi}/logs/estormi-daily-dag.log"
if [ -f "$_rotate_log" ]; then
  _size=$(stat -f %z "$_rotate_log" 2>/dev/null || stat -c %s "$_rotate_log" 2>/dev/null || echo 0)
  if [ "$_size" -gt $((10 * 1024 * 1024)) ]; then
    mv -f "$_rotate_log" "${_rotate_log}.1"
  fi
fi
unset _rotate_log _size


# ── log-line timestamps ────────────────────────────────────────
# Source the shared prefixer so every line of stdout+stderr from
# this point on carries an HH:MM:SS prefix in the DAG log.
_ts_helper="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/../packages/estormi_ingestion/shared/log_timestamps.sh"
[ -r "$_ts_helper" ] && . "$_ts_helper" || true
type _estormi_ts_prefix >/dev/null 2>&1 || _estormi_ts_prefix() { cat; }
# Tell shell connectors invoked from this DAG to NOT install their own
# in-process prefixer — the DAG already pipes each stage's output through
# the shared `_estormi_ts_prefix` (see `run_stage`), so a connector-side
# install would produce `[HH:MM:SS] [HH:MM:SS] …` double-prefixed lines.
# When the same connector script is run standalone (outside the DAG), the
# guard is unset, so it installs its own prefixer as before.
export _ESTORMI_LOG_TS_INSTALLED=1

# Resolve the repo root and load .env *before* anything reads MCP_SERVER_PORT:
# the engine-lock DB path is derived (via ESTORMI_DB_PATH / ESTORMI_DATA_DIR)
# from the same config the app uses, and .env is the documented place to set a
# non-default port (see .env.example). Sourcing it later would point this run at
# a different DB than the app, splitting the cross-process concurrency guard.
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
# shellcheck disable=SC1091
source .env 2>/dev/null || true
# NOTE: the cross-process engine lock (one engine at a time) is acquired further
# down, once $PY and $ESTORMI_DB_PATH are resolved — see "engine lock" below.

# Tee stdout to the canonical pipeline log when running interactively.
# When stdout is already a file (launchd StandardOutPath or _run_dag() server
# launch), write directly — tee is not needed and kill 0 in the EXIT trap
# would kill it before it flushes its stdio buffer, losing the last ~4 KB.
# Note: the inode-comparison approach is broken because $(stat /dev/fd/1)
# creates a command substitution that replaces fd 1 with a capture pipe,
# so stat always sees the pipe inode, never the log file inode.
_ESTORMI_DATA="${ESTORMI_DATA_DIR:-$HOME/Library/Application Support/Estormi}"
DAG_MAIN_LOG="${_ESTORMI_DATA}/logs/estormi-daily-dag.log"
mkdir -p "${_ESTORMI_DATA}/logs"
if [ -t 1 ]; then
  exec > >(tee -a "$DAG_MAIN_LOG")
fi

# Python selection precedence: bundled standalone > repo venv > system PATH.
# The bundle ships its own python-build-standalone at `python/` so the app
# can ingest without depending on a working machine Python. When running
# from a dev checkout, the venv comes next. We only fall through to the
# system Python on a bare checkout with no venv.
if [ -z "${PY:-}" ]; then
  if [ -x "$ROOT/python/bin/python3" ]; then
    PY="$ROOT/python/bin/python3"
  elif [ -x "$ROOT/.venv/bin/python3" ]; then
    PY="$ROOT/.venv/bin/python3"
  else
    PY="$(command -v python3)"
  fi
fi

# Per-stage logs live in the data dir, next to the main DAG log — the
# Settings UI resolves stage logs there and `/tmp` copies were both fragile
# (OS tmp reaping) and silently unlinked by the admin reset endpoints.
LOG_DIR="${_ESTORMI_DATA}/logs"
RUN_STAMP="$(date +%Y%m%d-%H%M%S)"

# Path to the canonical Estormi DB so the dag_state CLI knows where to write.
# memory_core.dag_state honours ESTORMI_DB_PATH first, then falls back to
# DATA_DIR/estormi.db computed from ESTORMI_DATA_DIR.
export ESTORMI_DATA_DIR="${ESTORMI_DATA_DIR:-$_ESTORMI_DATA}"
export ESTORMI_DB_PATH="${ESTORMI_DB_PATH:-$ESTORMI_DATA_DIR/estormi.db}"

# Helper: record a DAG lifecycle event. Echoes errors but never fails the DAG —
# observability must not block ingestion.
_dag_state() {
  "$PY" -m memory_core.dag_state "$@" 2>/tmp/estormi-dag-state.err || {
    echo "[dag] dag_state CLI '$1' failed (see /tmp/estormi-dag-state.err)" >&2
    return 0
  }
}

DAG_TRIGGER="${DAG_TRIGGER:-scheduled}"
DAG_ERR_LOG="${_ESTORMI_DATA}/logs/estormi-daily-dag-error.log"

# ── cross-process engine lock ──────────────────────────────────
# One engine at a time across processes: this shell-launched DAG, the app's
# in-process scheduler, and the briefing engine all coordinate through the same
# DB-backed lock (memory_core.engine_lock — it replaced the old /tmp pid file).
# We record pid==pgid==$$ to match the API server's killpg target exactly, and
# refuse to start if a live engine (of any kind) already holds the slot.
if ! "$PY" -m memory_core.engine_lock acquire \
      --kind ingestion --pid "$$" --pgid "$$" --source "$DAG_TRIGGER" >/dev/null; then
  echo "[dag] another engine already holds the lock; exiting" >&2
  exit 1
fi
# Release the lock on exit. On a signal also tear down the process group (stops
# the running stage). A hard SIGKILL skips the trap — steal-if-dead reclaims the
# slot next time. Mirrors server/jobs.py's release-on-kill.
_release_lock() {
  "$PY" -m memory_core.engine_lock release --kind ingestion --pid "$$" >/dev/null 2>&1 || true
}
trap '_release_lock; kill 0' TERM INT
trap '_release_lock' EXIT

DAG_RUN_ID="$(_dag_state start-run \
  --trigger "$DAG_TRIGGER" \
  --engine ingestion \
  --log-path "$DAG_MAIN_LOG" \
  --err-path "$DAG_ERR_LOG")"
DAG_RUN_ID="${DAG_RUN_ID:-0}"

# Tracks the dag_stages row for whatever stage is currently in-flight, so the
# TERM/INT trap can close it as `failed` before `kill 0` brings the script
# down. Without this, an interrupted DAG leaves both the run and its in-flight
# stage stuck in `running` forever, and the UI reads that as a crashed job.
CURRENT_STAGE_ID=0

# Defined here (empty) so the TERM/INT trap's `rm -rf "$STATUS_DIR"` is always
# provably safe, even for a signal arriving before STATUS_DIR is mktemp'd below.
STATUS_DIR=""

# Mark the in-flight run/stage as interrupted in the DB, clean up, then kill
# the whole process group. Called from the TERM/INT trap. Best-effort: every
# step swallows errors so a partially-recorded run is still better than a
# zombie one.
_dag_on_interrupt() {
  if [ "${CURRENT_STAGE_ID:-0}" != "0" ]; then
    # A preempted stage is not a failed stage — the connector never got to
    # report a result. Mark it ``cancelled`` (distinct from ``failed``) so
    # the UI doesn't show a misleading red "FAILED" pill with no error.
    _dag_state finish-stage \
      --stage-id "$CURRENT_STAGE_ID" \
      --status cancelled \
      --stderr-excerpt "Preempted: stage was killed by stop/restart before it could finish." \
      >/dev/null 2>&1 || true
  fi
  if [ "${DAG_RUN_ID:-0}" != "0" ]; then
    _dag_state finish-run \
      --run-id "$DAG_RUN_ID" \
      --status cancelled >/dev/null 2>&1 || true
  fi
  _release_lock
  rm -rf "${STATUS_DIR:-}"
  kill 0
}

# Promote to the run-aware trap as soon as DAG_RUN_ID exists, so a TERM during
# the early setup phase (after start-run, before STATUS_DIR is created) still
# closes the dag_runs row instead of leaving a zombie.
trap '_dag_on_interrupt' TERM INT

# The connector registry (connectors) is the single source of
# truth for which stages exist and how each one runs. `packages/` must be on
# PYTHONPATH so both `python -m connectors` and the `python -m
# estormi_ingestion.*` stage subprocesses resolve (both live under packages/).
export PYTHONPATH="$ROOT/packages${PYTHONPATH:+:$PYTHONPATH}"

# Default stage list = `connectors stages` (the nightly run-all set).
# A `STAGES=` override is honoured verbatim, exactly as before.
if [ -n "${STAGES:-}" ]; then
  STAGES_LIST="$STAGES"
else
  STAGES_LIST="$("$PY" -m connectors stages | tr '\n' ' ')"
fi
read -r -a STAGES_ARR <<<"$STAGES_LIST"

# A registry that resolves to zero stages means `connectors stages` failed
# (bad PYTHONPATH, import error) — don't report a green "success" run that
# ingested nothing. An explicit empty STAGES= override is the caller's choice.
if [ -z "${STAGES:-}" ] && [ "${#STAGES_ARR[@]}" -eq 0 ]; then
  echo "[dag] no stages resolved from the connector registry — aborting" >&2
  exit 1
fi


START_TS=$(date +%s)
echo "[dag] starting daily ingestion DAG at $(date +%Y-%m-%dT%H:%M:%S%z)"
echo "[dag] stages: ${STAGES_ARR[*]}"
echo "[dag] log: $DAG_MAIN_LOG"

# Temp dir for per-stage exit status + duration (subshells can't write parent arrays)
STATUS_DIR="$(mktemp -d /tmp/estormi-dag-status-XXXXXX)"
# On normal exit: only clean up — do NOT kill 0 here.  If tee is running
# (interactive terminal launch), killing it before bash closes its pipe would
# prevent tee from flushing its stdio buffer, losing the last lines of the log.
# Bash will close the pipe naturally on exit, sending EOF to tee, which then
# flushes and exits cleanly.
trap '_release_lock; rm -rf "$STATUS_DIR"' EXIT

# Runs a stage in the current shell; writes result to STATUS_DIR/<stage>
run_stage() {
  local stage="$1"
  local stage_log="$LOG_DIR/estormi-stage-${RUN_STAMP}-${stage}.log"
  local s_start s_end dur status rc stage_id db_status
  s_start=$(date +%s)
  status="ok"
  rc=0
  echo "[dag] === $stage starting ($(date +%H:%M:%S)) ==="
  echo "[dag] stage-log:${stage}: ${stage_log}"
  stage_id="$(_dag_state start-stage \
    --run-id "$DAG_RUN_ID" \
    --stage "$stage" \
    --log-path "$stage_log")"
  stage_id="${stage_id:-0}"
  CURRENT_STAGE_ID="$stage_id"
  # Pipe the connector's stdout/stderr through the shared HH:MM:SS prefixer
  # before writing to the stage log. ShellConnectors (notes, mail, gcal, …)
  # used to install their own prefixer inside watch_and_ingest.sh; exporting
  # `_ESTORMI_LOG_TS_INSTALLED` (below) makes them skip that, so the DAG
  # owns the single canonical prefix point and ScriptConnectors (code,
  # documents, knowledge, …) — which print raw lines from Python and
  # otherwise produced un-timestamped logs — get the same treatment.
  # Stderr is also tee'd through the same prefixer into DAG_ERR_LOG so the
  # UI's "Open error log" button has real content — without the tee, errors
  # only landed in the per-stage log and the DAG-level err file stayed empty.
  # `${PIPESTATUS[0]}` captures the connector's rc, not the prefixer's.
  stage_cmd_exec "$stage" \
    2> >(_estormi_ts_prefix | tee -a "$DAG_ERR_LOG" >&2) \
    | _estormi_ts_prefix >>"$stage_log"
  rc=${PIPESTATUS[0]}
  # Exit 75 (EX_TEMPFAIL) is the connector's "skipped — missing macOS
  # permission" signal (see connectors/permission_gate.py). It is a
  # clean skip, not a failure: no error tail, don't mark the run failed —
  # record the stage as `skipped` so the UI shows "needs permission" instead
  # of an error.
  if [ "$rc" -eq 75 ]; then
    status="skipped"
    echo "[dag] --- $stage skipped (missing permission) ---"
  elif [ "$rc" -ne 0 ]; then
    # Append the per-stage log tail to DAG_ERR_LOG when this stage failed so
    # the err log captures the full picture (stdout context, not just stderr).
    {
      printf '\n=== stage %s failed (rc=%d) — tail of %s ===\n' "$stage" "$rc" "$stage_log"
      tail -n 100 "$stage_log" 2>/dev/null || true
    } >>"$DAG_ERR_LOG"
    status="fail"
    echo "[dag] !!! $stage FAILED — see $stage_log" >&2
  fi
  s_end=$(date +%s)
  dur=$((s_end - s_start))
  echo "[dag] === $stage $status (${dur}s) ==="
  db_status="ok"
  [ "$status" = "fail" ] && db_status="failed"
  [ "$status" = "skipped" ] && db_status="skipped"
  if [ "$stage_id" != "0" ] && [ -n "$stage_id" ]; then
    _dag_state finish-stage \
      --stage-id "$stage_id" \
      --status "$db_status" \
      --exit-code "$rc" \
      --duration-ms "$((dur * 1000))" >/dev/null
  fi
  CURRENT_STAGE_ID=0
  printf '%s %d\n' "$status" "$dur" > "$STATUS_DIR/$stage"
}

# Executes the command for a given stage name via the connector registry.
# The registry owns the per-stage run logic (shell script vs. python
# ingester, env vars, --root flags) — see connectors/.
stage_cmd_exec() {
  "$PY" -m connectors run "$1"
}

# Background wrapper: run a stage in its own subshell with a self-cancelling
# trap. The parent's TERM/INT trap (_dag_on_interrupt) only closes the *run*
# row in parallel mode — each backgrounded stage owns its own dag_stages row,
# so it must mark that row `cancelled` when the parent's `kill 0` preempts it.
# CURRENT_STAGE_ID is set by run_stage inside this same subshell.
run_stage_bg() {
  trap '[ "${CURRENT_STAGE_ID:-0}" != "0" ] && _dag_state finish-stage --stage-id "$CURRENT_STAGE_ID" --status cancelled --stderr-excerpt "Preempted: stage was killed by stop/restart before it could finish." >/dev/null 2>&1; exit 143' TERM INT
  run_stage "$1"
}

# ── Run ingestion stages with bounded parallelism ─────────────────────────────
# bash 3.2 (Apple's /bin/bash) has no `wait -n`, so the slot gate polls the set
# of launched PIDs with `kill -0` and drops the ones that have exited. Counting
# with `kill -0` can only *over*-count a just-exited-but-unreaped child for up to
# one poll, which merely delays the next launch — it can never exceed the cap,
# the safe direction on a personal Mac.
INGEST_PARALLELISM="${ESTORMI_INGEST_PARALLELISM:-3}"
case "$INGEST_PARALLELISM" in (''|*[!0-9]*) INGEST_PARALLELISM=3 ;; esac
[ "$INGEST_PARALLELISM" -lt 1 ] && INGEST_PARALLELISM=1

if [ "$INGEST_PARALLELISM" -le 1 ]; then
  for stage in "${STAGES_ARR[@]}"; do
    run_stage "$stage"
  done
else
  echo "[dag] running stages with up to ${INGEST_PARALLELISM} in parallel"
  RUNNING_PIDS=""   # currently-alive stage PIDs (pruned each iteration)
  ALL_PIDS=""       # every launched stage PID (for the final wait)
  for stage in "${STAGES_ARR[@]}"; do
    # Block until a concurrency slot frees up.
    while : ; do
      alive=""
      n=0
      for p in $RUNNING_PIDS; do
        if kill -0 "$p" 2>/dev/null; then
          alive="$alive $p"
          n=$((n + 1))
        fi
      done
      RUNNING_PIDS="$alive"
      [ "$n" -lt "$INGEST_PARALLELISM" ] && break
      # Sub-second reap: macOS /bin/bash (3.2) has no `wait -n`, so poll —
      # but a 1s sleep here added up to a second of dead air between stages.
      sleep 0.25
    done
    run_stage_bg "$stage" &
    pid=$!
    RUNNING_PIDS="$RUNNING_PIDS $pid"
    ALL_PIDS="$ALL_PIDS $pid"
  done
  # Reap each launched stage explicitly (a bare `wait` would also block on the
  # `tee` from the interactive `exec > >(tee …)` process substitution).
  for p in $ALL_PIDS; do
    wait "$p" 2>/dev/null || true
  done
fi

# ── Collect results ───────────────────────────────────────────────────────────
STAGE_NAMES=()
STAGE_STATUS=()
STAGE_DURATIONS=()
for stage in "${STAGES_ARR[@]}"; do
  if [ -f "$STATUS_DIR/$stage" ]; then
    read -r st dur < "$STATUS_DIR/$stage"
  else
    st="fail"; dur=0
  fi
  STAGE_NAMES+=("$stage")
  STAGE_STATUS+=("$st")
  STAGE_DURATIONS+=("$dur")
done


END_TS=$(date +%s)
TOTAL=$((END_TS - START_TS))

SUMMARY=""
FAILED=0
i=0
while [ $i -lt ${#STAGE_NAMES[@]} ]; do
  if [ "${STAGE_STATUS[$i]}" = "fail" ]; then
    SUMMARY+="✗${STAGE_NAMES[$i]} "
    FAILED=$((FAILED + 1))
  else
    SUMMARY+="✓${STAGE_NAMES[$i]} "
  fi
  i=$((i + 1))
done

echo "[dag] === DONE in ${TOTAL}s — $SUMMARY ==="

if [ "$DAG_RUN_ID" != "0" ] && [ -n "$DAG_RUN_ID" ]; then
  DAG_FINAL_STATUS="ok"
  [ "$FAILED" -gt 0 ] && DAG_FINAL_STATUS="failed"
  _dag_state finish-run \
    --run-id "$DAG_RUN_ID" \
    --status "$DAG_FINAL_STATUS" \
    --duration-ms "$((TOTAL * 1000))" >/dev/null
fi

if command -v osascript &>/dev/null; then
  if [ "$FAILED" -gt 0 ]; then
    osascript -e "display notification \"$FAILED stage(s) failed in ${TOTAL}s — see $DAG_MAIN_LOG\" with title \"Estormi — DAG\"" 2>/dev/null || true
  else
    osascript -e "display notification \"All stages OK (${TOTAL}s)\" with title \"Estormi — DAG\"" 2>/dev/null || true
  fi
fi
