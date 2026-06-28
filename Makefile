PYTHON ?= $(shell if [ -x .venv/bin/python3 ]; then printf '%s' .venv/bin/python3; else command -v python3 || printf '%s' python3; fi)
PYTEST ?= $(shell if [ -x .venv/bin/pytest ]; then printf '%s' .venv/bin/pytest; else command -v pytest || printf '%s' pytest; fi)
# Prefer the .venv ruff (pinned == the CI/pre-commit version in
# tests/requirements-test.txt) over a stray PATH ruff, so `make lint` — the sole
# gate while CI billing is off — runs the exact version CI would. `make lint`
# fails loudly if the resolved ruff is not the pinned version.
RUFF   ?= $(shell if [ -x .venv/bin/ruff ]; then printf '%s' .venv/bin/ruff; else command -v ruff || printf '%s' ruff; fi)
RUFF_VERSION := 0.15.16
VENV    := $(PYTHON)

PYTHON_STANDALONE_URL := https://github.com/astral-sh/python-build-standalone/releases/download/20250409/cpython-3.12.10+20250409-aarch64-apple-darwin-install_only.tar.gz
# Canonical SHA256 of the tarball above, pinned from the upstream release's
# per-file `.sha256` sidecar (mirrored in the `SHA256SUMS` asset) for tag
# 20250409. Re-verify on any URL/tag bump:
#   curl -fsSL $(PYTHON_STANDALONE_URL).sha256
PYTHON_STANDALONE_SHA256_DEFAULT := 2d6477ecd10191675b7e7979e4b9e811fef36833ef3a7f3aa445eec305ce59a2
# `make bundle-python` verifies the download against this by default. Override on
# the CLI for a different tarball:  make bundle PYTHON_STANDALONE_SHA256=<sha256>
# The ONLY way to skip verification is ESTORMI_TRUST_PYTHON_STANDALONE=1 (exact
# value "1"), and it is refused outright when CI is set. It is a loudly-warned
# offline-development escape hatch — never for a release or distributed build.
PYTHON_STANDALONE_SHA256 ?= $(PYTHON_STANDALONE_SHA256_DEFAULT)

# Code-signing identity for the macOS bundle. A real identity gives a stable
# designated requirement (cert + bundle id), so macOS keeps the app's TCC grants
# (Full Disk Access, and the sidecar's Contacts/Calendar/Reminders) across
# rebuilds — an ad-hoc signature is cdhash-based and resets them every build. The
# `bundle` target signs the finished bundle with this identity but WITHOUT the
# hardened runtime: the hardened runtime stops the Python sidecar from presenting
# the macOS Contacts prompt, and is only needed for notarization (not done here).
# APPLE_SIGNING_IDENTITY (env) wins; otherwise auto-detect from the keychain,
# preferring a Developer ID Application cert. The cert's SHA-1 hash (40 hex) is
# matched — no parens, so it is $(shell)-safe.
CODESIGN_ID := $(or $(APPLE_SIGNING_IDENTITY),$(shell security find-identity -v -p codesigning 2>/dev/null | grep "Developer ID Application" | grep -oE '[0-9A-F]{40}' | head -1),$(shell security find-identity -v -p codesigning 2>/dev/null | grep -oE '[0-9A-F]{40}' | head -1))

.DEFAULT_GOAL := help
.PHONY: help start

## ── Core ──────────────────────────────────────────────────────────────────
help: ## List available targets, grouped by section
	@grep -hE '^## |^[a-zA-Z0-9_-]+:.*?## ' $(MAKEFILE_LIST) | \
	  awk 'BEGIN {FS = ":.*?## "} \
	       /^## / { sub(/^## /, ""); printf "\n\033[1m%s\033[0m\n", $$0; next } \
	       { printf "  \033[36m%-20s\033[0m %s\n", $$1, $$2 }'

start: ## Serve the local FastAPI app on MCP_SERVER_PORT (default: 8000)
	@cli_host="$${MCP_SERVER_HOST:-}"; cli_port="$${MCP_SERVER_PORT:-}"; \
	set -a; [ ! -f .env ] || . ./.env; set +a; \
	MCP_SERVER_HOST="$${cli_host:-$${MCP_SERVER_HOST:-127.0.0.1}}"; \
	MCP_SERVER_PORT="$${cli_port:-$${MCP_SERVER_PORT:-8000}}"; \
	.venv/bin/uvicorn estormi_server.main:app \
	  --host "$$MCP_SERVER_HOST" \
	  --port "$$MCP_SERVER_PORT" \
	  --app-dir packages

# Targets are organized into thematic includes under make/. `make help`
# still lists everything (it greps $(MAKEFILE_LIST), which spans them all).
include $(wildcard make/*.mk)
