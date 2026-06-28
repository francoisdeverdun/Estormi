# Tests, health checks, QA badges — see docs/testing.md
.PHONY: health weekly-report dashboard test-search test-local test-suite test test-unit test-integration test-e2e test-contract test-fast test-performance test-metrics test-frontend test-e2e-frontend test-rust

## ── Tests ────────────────────────────────────────────────────────────────

health: ## Quick service health check + data freshness per source
	bash scripts/health_check.sh
	$(VENV) scripts/freshness_check.py

weekly-report: ## Generate freshness report via Claude CLI (saved to ~/estormi-reports/)
	bash scripts/weekly_report.sh

dashboard: ## Open the dashboard in the default browser
	@open "http://localhost:$${MCP_SERVER_PORT:-8000}/"

test-search: ## Smoke-test /search_memory
	@set -a; . ./.env; set +a; \
	curl -s -X POST http://localhost:$${MCP_SERVER_PORT:-8000}/search_memory \
	  -H "Content-Type: application/json" \
	  -d '{"query":"test", "limit":3}' | python3 -m json.tool

test-local: ## End-to-end smoke test (ingest fixtures + search)
	$(VENV) scripts/smoke_test.py

test-suite: ## Hermetic runtime validation (server + synthetic sources). See docs/testing.md
	bash scripts/test_suite.sh

# Coverage floor rebaselined 68 -> 66 after the two-engine simplification: deleting
# the Extraction + Correlation engines (~21k lines, well-tested) removed more
# covered lines than uncovered, so the pre-existing low-coverage ingestion
# connectors now weigh more and the aggregate settled at ~67%. Ratcheted 66 -> 70
# (code-graph store 17% -> 96%), then 70 -> 75 once the aggregate held above 76%
# (the iMessage / Google-Calendar-auth / reminders / http-client / knowledge-
# sources modules each brought to ~90%+). Ratcheted 75 -> 76 once the aggregate
# held above 76.9% (whoop_oauth OAuth routes 41% -> 99%). Ratcheted 76 -> 77 once
# the aggregate held above 77.5% (knowledge/briefing API routes 52% -> 92%).
# Ratcheted 77 -> 80 once the aggregate held above 80% (whatsapp service 44% ->
# 89%, pipeline API 53% -> 91%, audit error-paths). The floor tracks the
# sustained level so it can only move up.
# Badge regeneration is deliberately NOT chained here: the SVGs under
# assets/badges/ are committed, so rewriting them on every local run left
# contributors with a dirty tree after `make test`. CI does not push them
# either (it uploads them as an artifact only); the maintainer regenerates and
# commits them by hand via `make test-metrics` before a release.
test: ## Run the full pytest suite with coverage (estormi_server + packages + ingestion + briefing)
	@mkdir -p build/coverage
	@# `-m 'not performance'`: the performance/ benchmarks are wall-clock latency
	@# thresholds that add no unique coverage and can trip on a loaded machine.
	@# They are the deliberate fifth layer — run them via `make test-performance`.
	@# ESTORMI_GATE=1 flips the real-embeddings e2e warmup from skip→fail, so a
	@# missing model cache can't silently turn the only real Qdrant gate green.
	ESTORMI_GATE=1 $(PYTEST) tests/ -m 'not performance' --tb=short -q --cov=estormi_server --cov=memory_core --cov=connectors --cov=estormi_ingestion --cov=estormi_briefing --cov=estormi_distill --cov-report=term-missing --cov-report=json:build/coverage/coverage.json --cov-fail-under=80

test-unit: ## Run every unit-marked test, wherever it lives in tests/
	$(PYTEST) tests/ -q -m unit

test-integration: ## Run every integration-marked test, wherever it lives in tests/
	$(PYTEST) tests/ -q -m integration

test-e2e: ## Run every e2e-marked test, wherever it lives in tests/
	$(PYTEST) tests/ -q -m e2e

test-contract: ## Run docs, workflow, and repository contract tests
	$(PYTEST) tests/ -q -m contract

test-fast: ## Run full suite except slow/E2E/performance tests
	$(PYTEST) tests/ -q -m "not slow and not e2e and not performance"

test-performance: ## Run performance benchmarks (latency/throughput thresholds)
	$(PYTEST) tests/ -q -m performance -v

test-metrics: ## Refresh QA badges from an existing build/coverage/coverage.json
	$(VENV) scripts/qa_metrics.py build/coverage/coverage.json assets/badges

test-frontend: ## Run the SPA + design-system vitest suites
	@pnpm --filter @estormi/ui-kit test
	@pnpm --filter @estormi/web-ui test

test-e2e-frontend: ## Run the SPA Playwright e2e suite (stubbed backend; needs `playwright install`)
	pnpm --filter @estormi/web-ui test:e2e

test-rust: ## Run the Rust unit tests for the macOS Tauri shell (cargo test)
	cd apps/estormi-macos && cargo test
