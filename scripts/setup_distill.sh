#!/bin/zsh
# Estormi — one-shot setup for the Distillation engine's tooling.
#
# The distillation chain (QLoRA train → fuse → GGUF) runs against an MLX
# toolkit that is NOT bundled with the app (Apple-Silicon-only, ~1 GB of
# wheels, and most users never distill). This script installs it under the
# data dir, where estormi_distill.trainer.tooling() probes for it:
#
#   <data dir>/distill/tools/venv        — python venv with mlx-lm + gguf deps
#   <data dir>/distill/tools/llama.cpp   — shallow clone (convert_hf_to_gguf.py)
#   llama-quantize                       — via Homebrew (llama.cpp formula)
#
# Idempotent: safe to re-run after an update. Everything under the data dir
# is personal-data territory — the models and adapters the engine later
# produces there memorize the user's life and must never leave the machine
# unencrypted.
set -euo pipefail

if [ "$(uname -sm)" != "Darwin arm64" ]; then
    echo "✗ Distillation tooling is Apple-Silicon-only (MLX)." >&2
    exit 1
fi

# Resolve the library location exactly as the engine does — honoring the
# relocatable root-storage pointer file (memory_core.datadir), not just $HOME —
# so a relocated library installs the tooling where trainer.tooling() probes.
# Falls back to ESTORMI_DATA_DIR / the default config home when the project venv
# isn't available to run the resolver.
_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DATA_DIR="$(
    PYTHONPATH="$_ROOT/packages" "$_ROOT/.venv/bin/python" -c \
        'from memory_core.settings import resolve_data_dir; print(resolve_data_dir())' \
        2>/dev/null
)" || true
DATA_DIR="${DATA_DIR:-${ESTORMI_DATA_DIR:-$HOME/Library/Application Support/Estormi}}"
TOOLS="$DATA_DIR/distill/tools"
mkdir -p "$TOOLS"

echo "── Estormi distillation tooling → $TOOLS"

echo "[1/3] MLX venv (mlx-lm + GGUF-conversion deps)…"
if [ ! -x "$TOOLS/venv/bin/python" ]; then
    python3 -m venv "$TOOLS/venv"
fi
"$TOOLS/venv/bin/pip" install -q --upgrade mlx-lm gguf sentencepiece torch
"$TOOLS/venv/bin/python" -c "import mlx_lm; print('    mlx-lm', getattr(mlx_lm, '__version__', 'ok'))"

echo "[2/3] llama.cpp converter (shallow clone)…"
if [ ! -f "$TOOLS/llama.cpp/convert_hf_to_gguf.py" ]; then
    rm -rf "$TOOLS/llama.cpp"
    git clone --depth 1 https://github.com/ggml-org/llama.cpp "$TOOLS/llama.cpp"
else
    git -C "$TOOLS/llama.cpp" pull --ff-only 2>/dev/null || true
fi

echo "[3/3] llama-quantize…"
if ! command -v llama-quantize >/dev/null 2>&1 && [ ! -x /opt/homebrew/bin/llama-quantize ]; then
    if command -v brew >/dev/null 2>&1; then
        brew install llama.cpp
    else
        echo "✗ llama-quantize missing and Homebrew not found." >&2
        echo "  Install Homebrew (https://brew.sh) then re-run, or put a" >&2
        echo "  llama-quantize binary on PATH." >&2
        exit 1
    fi
fi
echo "    $(command -v llama-quantize || echo /opt/homebrew/bin/llama-quantize)"

echo ""
echo "✓ Distillation tooling ready — the Officina card's button is now live."
