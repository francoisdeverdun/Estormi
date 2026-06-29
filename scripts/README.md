# scripts

Operational and developer utility scripts. Everything lives flat at the top
level alongside the launchd plist; there is no deeper hierarchy.

The "Invoked by" column names the concrete trigger — a `make` target (defined
in `make/*.mk`), the launchd agent, a git hook, CI, or `manual` for scripts you
run by hand or that other scripts call.

## Daily pipeline & health

| Script | Purpose | Invoked by |
| --- | --- | --- |
| `daily_ingestion.sh` | Run the full source ingestion DAG. | App APScheduler (`server/launchers/ingestion.py`); `make daily-dag` manually |
| `weekly_report.sh` | Freshness report — runs `freshness_check.py`, asks Claude for a narrative, saves to `~/estormi-reports/`. | `weekly-report` launchd agent; `make weekly-report` |
| `freshness_check.py` | Per-source data-freshness check (`--json` for machine output). | `make health`; `weekly_report.sh` |
| `health_check.sh` | Reachability check — MCP `/health`, Qdrant data dir, SQLite DB, and loaded LaunchAgents. | `make health` |
| `app.estormi.local.weekly-report.plist` | launchd agent template for the weekly report. | `make install-agents` (copies + loads) |

There is no daily-ingestion plist: the pipeline is scheduled in-process by the
app's APScheduler so its macOS permission grants attach to Estormi, not a
detached launchd job. `make install-agents` actively retires any stale
`app.estormi.local.daily-dag` agent left from earlier installs.

## QA badges, version & codegen

| Script | Purpose | Invoked by |
| --- | --- | --- |
| `qa_metrics.py` | Refresh `assets/badges/{coverage,tests,qa-layers}.svg` from pytest collection + `build/coverage/coverage.json`. | `make test-metrics`; CI |
| `set_version.py` | Set the macOS app version everywhere in one command. | `make set-version V=X.Y.Z` |
| `gen_openapi.py` | Generate the canonical OpenAPI spec from the live FastAPI app (`--check` verifies it is current). | `make openapi` / `make openapi-check` |

## Security gates

| Script | Purpose | Invoked by |
| --- | --- | --- |
| `security_scan.py` | Repo-local secret/PII guardrail over staged (or `--include-untracked` / `--history`) content. | pre-commit hook; CI |
| `detect_secrets_gate.py` | Fail when detect-secrets reports findings new to the baseline. | pre-commit; CI |
| `setup_branch_protection.sh` | Apply GitHub branch protection to `main` once the repo is public (one-shot). | manual |

## Runtime validation

| Script | Purpose | Invoked by |
| --- | --- | --- |
| `smoke_test.py` | End-to-end smoke test: seed 3 fixtures, probe semantics, clean up. | `make test-local` |
| `test_suite.sh` | Hermetic end-to-end validation — starts its own server against a temp data dir, seeds synthetic sources, probes retrieval. | `make test-suite` |
| `reset_data.py` | Wipe the Qdrant collection + truncate SQLite chunks and watermarks (forces full re-ingest). | `make reset` |

## Build, install & assets

| Script | Purpose | Invoked by |
| --- | --- | --- |
| `build.sh` | The single rebuild + install entrypoint: `make bundle` then kill → atomic swap → relaunch → health-check. | manual |
| `install.sh` | Install `Estormi.app` on macOS (staged into `Estormi.zip` for downloaders). | `make bundle` (staged); end users |
| `setup.sh` | One-shot bootstrap for new clones and demos. | manual |
| `generate_icons.sh` | Build the macOS app icon set + `icon.icns` from `assets/brand/estormi-app-icon.png`. | `make bundle` |
| `generate_caps.py` | Generate illuminated drop-cap lettrines (`estormi-cap-<L>-<tone>.svg`). | manual |
| `fix_python_shebangs.sh` | Rewrite bundled-Python shebangs to the install location (idempotent). | `make bundle-python` / `make bundle` |

## Developer setup helpers

| Script | Purpose | Invoked by |
| --- | --- | --- |
| `setup-graphify-skill.sh` | Install the `graphify` CLI, seed `graphify-out/`, and set `core.hooksPath=.githooks`. | manual (run once per clone) |
| `setup_distill.sh` | One-shot setup for the Distillation engine's tooling (Apple Silicon). | manual |
| `vendor_fonts.py` | Vendor the brand webfonts (downloads the slim `.woff2` Latin subset). | manual |
| `run_prompt.sh` | Run a companion prompt from `prompts/companion/<slug>.md` through the Claude CLI. | `make prompt-*` targets |
