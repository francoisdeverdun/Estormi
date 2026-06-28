#!/bin/zsh
# Estormi — rebuild, install, relaunch
set -euo pipefail

# Resolve repo root from this script's own location (portable across machines).
# This script lives in scripts/, so the repo root is its parent (`:h:h`).
REPO_ROOT="${0:A:h:h}"
cd "$REPO_ROOT"

# shellcheck disable=SC1091
source .env 2>/dev/null || true

echo ""
echo "╔══════════════════════════════════════╗"
echo "║   Estormi — Build & Install         ║"
echo "╚══════════════════════════════════════╝"
echo ""

# 1. Commit pending changes — opt-in only. Unattended `git add -A` would
# sweep up half-finished work, secrets, etc. Set ESTORMI_AUTO_COMMIT=1 to
# re-enable for one-off rebuild scripts.
if [ "${ESTORMI_AUTO_COMMIT:-0}" = "1" ]; then
    echo "[1/4] Committing pending changes (ESTORMI_AUTO_COMMIT=1)..."
    git add -A 2>/dev/null || true
    git commit -m "chore: pre-build sweep" 2>/dev/null || true
else
    echo "[1/4] Auto-commit disabled (export ESTORMI_AUTO_COMMIT=1 to enable it)."
fi

# 2. Build Tauri app (via `make bundle` so the SPA is rebuilt too).
# `make bundle` is build-only — this script owns the kill + install + relaunch.
echo "[2/4] Building Tauri (make bundle)..."
export PATH="$HOME/.cargo/bin:$PATH"
make bundle 2>&1

# 3. Kill old instance BEFORE swapping the bundle. The Tauri binary is
# `estormi` (lowercase), so `pkill -x Estormi` never matched it — match the
# bundle path instead, which stops the app and both Python sidecars and
# frees port 8000. Killing first is load-bearing: the running estormi_server
# keeps dispatching engine subprocesses (ingestion stages, briefing) during
# the rebuild, and if we delete /Applications/Estormi.app before stopping it,
# those subprocesses fail with "can't open file" errors and the engine
# appears to be randomly dying.
echo "[3/4] Stopping the running instance..."
pkill -f '/Applications/Estormi.app' 2>/dev/null || true
sleep 2

# 4. Install to /Applications + relaunch. Only delete the installed copy
# AFTER we've confirmed the new bundle exists — otherwise a failed build
# would leave the user without an installed app.
#
# Staged + atomic swap: the install used to be `rm -rf ... && cp -r ...`,
# which left /Applications/Estormi.app in a half-copied state for the
# multi-second duration of `cp` (estormi_server/ gets copied before prompts/
# alphabetically). If anything relaunched the app during that window —
# the user clicking the dock icon, a launchd retry, a scheduled
# ingestion or briefing engine firing — the bundled Python would
# initialise on an incomplete tree and crash with TemplateNotFound on
# prompt files that did not yet exist on disk. Staging to .new + a single
# rename(2) collapses the inconsistency window to microseconds and keeps
# the previous complete bundle live the whole time the copy is running.
echo "[4/4] Installing to /Applications and relaunching..."
APP_SRC="$REPO_ROOT/apps/estormi-macos/target/aarch64-apple-darwin/release/bundle/macos/Estormi.app"
if [ ! -d "$APP_SRC" ]; then
    echo "    → aarch64 bundle not found, trying release/"
    APP_SRC="$REPO_ROOT/apps/estormi-macos/target/release/bundle/macos/Estormi.app"
fi
if [ -d "$APP_SRC" ]; then
    rm -rf /Applications/Estormi.app.new
    cp -R "$APP_SRC" /Applications/Estormi.app.new
    rm -rf /Applications/Estormi.app
    mv /Applications/Estormi.app.new /Applications/Estormi.app
    echo "    → Installed"
else
    echo "    → No Tauri bundle found — installation aborted"
    exit 1
fi
open /Applications/Estormi.app 2>/dev/null || open "$APP_SRC"

echo ""
echo "✓ Done. Checking the server in 10s..."
sleep 10
curl -s "http://127.0.0.1:${MCP_SERVER_PORT:-8000}/health" || echo "(server not ready yet)"
