#!/usr/bin/env bash
# Shared log-prefix helper sourced by the bash watch/ingest scripts.
#
# Wraps the script's stdout + stderr so every line written from this
# point on is prefixed with a HH:MM:SS timestamp. The bundled Estormi
# SPA reads these logs verbatim — having every line carry its own
# timestamp lets the user track per-line progress without having to
# diff file mtimes.
#
# Forks `date` once per line; at the throughput these connectors run
# (a handful of lines per second at most) that's well under the noise
# floor and avoids the bash 4.2+ `printf '%(...)T'` dependency.

_estormi_ts_prefix() {
  while IFS= read -r _ln; do
    printf '[%s] %s\n' "$(date '+%H:%M:%S')" "$_ln"
  done
}

# Only install the wrapper once per script invocation. The guard is
# script-local (no `export`) so child scripts can install their own
# prefixer when invoked from a parent that already has one.
#
# Why this matters: daily_ingestion.sh sources this helper, then runs
# per-stage subshells that redirect their output to per-stage log
# files (`>>$stage_log`). That redirect bypasses the pipeline-level
# prefixer, so each child needs its OWN exec-redirect to get
# timestamped lines in the per-stage log. If the guard were
# exported, the child would inherit ``_ESTORMI_LOG_TS_INSTALLED=1``
# from the parent and skip its install — the file would end up raw.
if [ -z "${_ESTORMI_LOG_TS_INSTALLED:-}" ]; then
  _ESTORMI_LOG_TS_INSTALLED=1
  exec > >(_estormi_ts_prefix) 2>&1
fi
