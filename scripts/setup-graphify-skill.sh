#!/usr/bin/env bash
# Wire up everything graphify needs to be a first-class repo citizen:
#   1. Install the `graphify` CLI (uv → pipx → pip).
#   2. Point git at the repo's .githooks/ so the pre-commit hook fires.
#   3. Seed graphify-out/graph.json (AST-only — no LLM key needed).
#
# Idempotent: re-running upgrades the CLI and skips anything already wired.
# Safe to invoke from `scripts/setup.sh` — every step degrades gracefully if
# its prerequisites are missing, so it never blocks the rest of setup.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

echo "=== graphify skill setup ==="

# ---------------------------------------------------------------------------
# 1. Install / upgrade the CLI.
# ---------------------------------------------------------------------------
# Prefer the project's own .venv (matches what estormi_server/requirements.txt
# already pins). Otherwise fall back to a user-wide install via uv / pipx.
if [ -x ".venv/bin/pip" ]; then
    echo "→ Installing graphifyy into .venv …"
    if command -v uv >/dev/null 2>&1; then
        uv pip install --python .venv/bin/python --upgrade 'graphifyy>=0.8.19'
    else
        .venv/bin/pip install --upgrade 'graphifyy>=0.8.19' >/dev/null
    fi
elif command -v uv >/dev/null 2>&1; then
    echo "→ Installing graphifyy via uv tool …"
    uv tool install --upgrade 'graphifyy>=0.8.19'
elif command -v pipx >/dev/null 2>&1; then
    echo "→ Installing graphifyy via pipx …"
    pipx install --force 'graphifyy>=0.8.19'
else
    echo "→ Installing graphifyy via pip --user (consider installing uv or pipx) …"
    if ! python3 -m pip install --user --upgrade 'graphifyy>=0.8.19'; then
        # An externally-managed Python (PEP 668) blocks --user. We do NOT
        # silently retry with --break-system-packages — that fights the OS
        # package manager and can corrupt the system Python. Require an
        # explicit opt-in instead, and prefer a venv/uv/pipx.
        if [ "${ESTORMI_PIP_BREAK_SYSTEM_PACKAGES:-}" = "1" ]; then
            echo "  ⚠ ESTORMI_PIP_BREAK_SYSTEM_PACKAGES=1 — retrying with --break-system-packages."
            python3 -m pip install --user --upgrade --break-system-packages 'graphifyy>=0.8.19'
        else
            echo "  pip --user failed (likely an externally-managed Python, PEP 668)."
            echo "  Install graphify into a venv, or via uv/pipx, instead. To force a"
            echo "  system-wide install anyway, re-run with ESTORMI_PIP_BREAK_SYSTEM_PACKAGES=1."
        fi
    fi
fi

# Pick the graphify binary we just installed.
GRAPHIFY=""
if [ -x ".venv/bin/graphify" ]; then
    GRAPHIFY=".venv/bin/graphify"
elif command -v graphify >/dev/null 2>&1; then
    GRAPHIFY="$(command -v graphify)"
fi

# ---------------------------------------------------------------------------
# 2. Point git at the repo's .githooks/ so the pre-commit hook fires.
# ---------------------------------------------------------------------------
if [ -d .git ] && [ -d .githooks ]; then
    CURRENT_HP="$(git config --get core.hooksPath || true)"
    if [ "$CURRENT_HP" != ".githooks" ]; then
        echo "→ Setting git core.hooksPath = .githooks"
        git config core.hooksPath .githooks
    fi
fi

# ---------------------------------------------------------------------------
# 3. Seed graphify-out/graph.json via the AST-only path.
# ---------------------------------------------------------------------------
# Use `update` instead of `extract` — it does the same code-AST pass without
# requiring an LLM API key, and is what the pre-commit hook runs anyway.
if [ -f "graphify-out/graph.json" ]; then
    echo "→ graphify-out/graph.json already present, skipping seed."
elif [ -n "$GRAPHIFY" ]; then
    echo "→ Seeding graphify-out/graph.json (AST-only, no LLM) …"
    mkdir -p graphify-out
    if ! "$GRAPHIFY" update . >graphify-out/.seed.log 2>&1; then
        echo "  Seed failed — see graphify-out/.seed.log."
    else
        echo "  Seed complete."
    fi
else
    echo "→ graphify binary not on PATH; run 'graphify update .' manually once it is."
fi

echo "✓ graphify skill ready."
