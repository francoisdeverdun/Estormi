# Lint, typecheck, dependency locks/audit, OpenAPI codegen, data reset/clean.
.PHONY: tokens tokens-check lint lint-frontend lint-rust typecheck typecheck-frontend check lock audit-deps openapi openapi-check reset clean clean-graph

## ── Dev ──────────────────────────────────────────────────────────────────

tokens: ## Regenerate iOS Tokens.swift from the canonical packages/ui-kit/src/tokens.css
	$(VENV) packages/ui-kit/gen_tokens_swift.py

tokens-check: ## Verify the committed iOS Tokens.swift is current (mirror of openapi-check)
	$(VENV) packages/ui-kit/gen_tokens_swift.py --check

lint: ## Ruff check + format check (pinned ruff; mirrors CI)
	@$(RUFF) --version 2>/dev/null | grep -q " $(RUFF_VERSION)$$" || { \
	  echo "lint: ruff $(RUFF_VERSION) required (the CI/pre-commit pin) but found '$$($(RUFF) --version 2>/dev/null || echo none)'."; \
	  echo "      Install it into the venv:  .venv/bin/pip install ruff==$(RUFF_VERSION)"; \
	  exit 1; }
	$(RUFF) check scripts packages tests
	$(RUFF) format --check scripts packages tests

lint-frontend: ## ESLint the web-ui SPA source (carries the no-dangerouslySetInnerHTML XSS guardrail)
	pnpm --filter @estormi/web-ui lint

lint-rust: ## Rust fmt-check + clippy for the macOS shell (mirrors CI rust.yml; uses the pinned nightly)
	@# Keeps the local gate honest about the Rust surface: committed source that
	@# fails `cargo fmt --check` (or trips clippy) would otherwise turn CI RED on
	@# push while `make check` stayed green. rustup honors the pinned nightly in
	@# apps/estormi-macos/rust-toolchain.toml.
	cd apps/estormi-macos && cargo fmt --check && cargo clippy --locked --all-targets -- -D warnings

typecheck: ## Pyright static type check (memory_core + estormi_server + connectors + estormi_distill; config in pyproject [tool.pyright])
	$(VENV) -m pyright

typecheck-frontend: ## tsc --noEmit across the JS workspace (ui-kit + web-ui)
	pnpm -r typecheck

# NB: `check` is the lint + typecheck + test gate across all three compiled
# surfaces (Python · JS · Rust). The bundle CVE audit (`make audit-deps`) is
# deliberately NOT a prerequisite — it needs network for the online advisory DB
# and the `pip-audit` tool — so it is a separate, documented pre-release step
# (see docs/release.md → "Before tagging"). The Playwright e2e (`make
# test-e2e-frontend`) and the Swift tests are also excluded here — they need a
# browser download / Xcode toolchain — and run in CI + the release.md
# native-surface checklist instead.
check: lint typecheck lint-frontend typecheck-frontend lint-rust test test-frontend test-rust ## Run the full local release gate (Python + JS + Rust lint/typecheck/test)

lock: ## Recompile requirements/requirements.lock from the dev + test floors (run after editing either requirements file)
	uv pip compile packages/estormi_server/requirements.txt tests/requirements-test.txt --universal --generate-hashes -o requirements/requirements.lock

audit-deps: ## Audit the SHIPPED bundle pins (requirements/requirements-bundle.txt) for known CVEs
	@# CI's security workflow audits the loose contributor floors
	@# (estormi_server/requirements.txt); this audits the exact `==` pins that
	@# actually ship in the macOS bundle's embedded interpreter — runnable
	@# locally so a known-vulnerable shipped dep is caught before a release even
	@# while CI is disabled. Needs network for the advisory DB.
	@$(VENV) -m pip_audit --version >/dev/null 2>&1 || { echo "pip-audit not installed — run: .venv/bin/pip install pip-audit"; exit 1; }
	@# diskcache GHSA-w8v5-vhqr-4h9v has NO fixed release published yet, so it
	@# cannot be patched by a bump. Ignored here with a paper trail; drop the
	@# --ignore-vuln once diskcache ships a fix (re-check on each release).
	$(VENV) -m pip_audit -r requirements/requirements-bundle.txt --ignore-vuln GHSA-w8v5-vhqr-4h9v

openapi: ## Regenerate the canonical OpenAPI spec + TS client types from the FastAPI app
	$(VENV) scripts/gen_openapi.py
	pnpm --filter @estormi/web-ui gen:api

openapi-check: ## Verify the committed OpenAPI spec is current (run after changing routes/models)
	$(VENV) scripts/gen_openapi.py --check

reset: ## Wipe Qdrant collection + truncate SQLite chunks and watermarks (forces full re-ingest; keeps settings only)
	$(VENV) scripts/reset_data.py

clean: ## DESTRUCTIVE — remove caches and shadow build dirs
	find . -name '__pycache__' -type d -exec rm -rf {} +
	rm -rf .ruff_cache .pytest_cache .grimp_cache .import_linter_cache
	rm -rf .coverage build/coverage
	rm -rf packages/*/build packages/*/*.egg-info
	@# Sweep stray .DS_Store from the source tree only — skip the big vendored
	@# trees (.venv/python/node_modules/target) and sibling worktrees so this
	@# stays fast and never touches another live session's checkout.
	find . \( -path ./.venv -o -path ./python -o -path ./node_modules -o -path ./apps/estormi-macos/target -o -path ./.claude/worktrees \) -prune -o -name '.DS_Store' -exec rm -f {} +

clean-graph: ## Remove the local graphify build (forces a full graph rebuild on next query)
	rm -rf graphify-out
