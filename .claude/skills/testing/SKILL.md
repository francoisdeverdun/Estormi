---
name: testing
description: 'Write and maintain tests for Estormi (tests/). USE FOR: adding tests, fixing failures, improving coverage, understanding async fixtures, mocking SQLite/Qdrant/LLMs, and validating docs/rendering behavior. Always run make test after changes.'
---

# Testing — Test Suite Development

## When to Use

- Writing or updating tests for any Estormi module.
- Fixing broken or flaky tests.
- Understanding DB, Qdrant, embedding, keyring, subprocess, or LLM mocks.
- Improving coverage.
- Adding regression tests for bugs.

## Quick Reference

```bash
make test
make test-unit
make test-integration
make test-contract
make test-fast
.venv/bin/pytest tests/estormi_server/test_tools.py -v
.venv/bin/pytest tests/estormi_server/test_tools.py::TestSearchMemory -v
.venv/bin/pytest tests/ -m 'not performance' --tb=short -q --cov=estormi_server --cov=memory_core --cov=connectors --cov=estormi_ingestion --cov=estormi_briefing --cov=estormi_distill --cov-report=term-missing --cov-report=json:build/coverage/coverage.json
```

`make test` currently expands to (`make/test.mk`):

```bash
ESTORMI_GATE=1 .venv/bin/pytest tests/ -m 'not performance' --tb=short -q --cov=estormi_server --cov=memory_core --cov=connectors --cov=estormi_ingestion --cov=estormi_briefing --cov=estormi_distill --cov-report=term-missing --cov-report=json:build/coverage/coverage.json --cov-fail-under=80
```

`ESTORMI_GATE=1` flips the real-embeddings e2e warmup from skip→fail, so a
missing model cache can't silently turn the only real Qdrant gate green.
`-m 'not performance'` excludes the wall-clock benchmarks (run those via `make
test-performance`). Coverage spans six Python roots (`estormi_server`,
`memory_core`, `connectors`, `estormi_ingestion`, `estormi_briefing`,
`estormi_distill`) and the run fails below the `--cov-fail-under` floor. Badge
writing is a separate step: `make test-metrics` (or `scripts/qa_metrics.py build/coverage/coverage.json
assets/badges`); it is not part of `make test` so a local run doesn't rewrite
committed badges. Raise the floor as coverage improves; never lower it to make a
red build pass.

## Other suites

This skill covers the Python pytest suite. The non-Python suites live
elsewhere:

- **Frontend** (Vitest) in `packages/web-ui/` and `packages/ui-kit/` —
  `make test-frontend` runs `pnpm --filter @estormi/ui-kit test` and
  `pnpm --filter @estormi/web-ui test`. The Playwright e2e specs live under
  `packages/web-ui/e2e/`; run them with `make test-e2e-frontend` (needs a
  one-time `playwright install`). In CI, `js.yml` runs every package's Vitest
  (`pnpm -r test`) and the web-ui Playwright e2e.
- **iOS** unit tests — the `EstormiTests` target (`apps/estormi-ios/Tests/`),
  run from Xcode and in CI via `.github/workflows/ios.yml`.
- **Embedded-mode e2e** — `tests/e2e/test_search_roundtrip_real.py` exercises
  search against a real (locally-running) Qdrant rather than the mocked boundary.

## Test Architecture

Every test carries exactly one **layer marker** — the marker, not the
directory, decides which `make` target runs it:

| Marker | Purpose |
|---|---|
| `unit` | Pure helpers and tooling; no app/server or external boundary. |
| `integration` | SQLite/runtime contracts and mocked service boundaries. |
| `e2e` | Public API flows that represent user/assistant scenarios. |
| `contract` | Docs, Makefile, schema, and CI-workflow contracts. |
| `performance` | Latency/throughput benchmarks with explicit thresholds. |

`make test-unit` / `test-integration` / `test-e2e` / `test-contract` select by
marker across the *whole* `tests/` tree; together they cover the entire suite.
Set the marker with a module-level `pytestmark = pytest.mark.<layer>` (use a
list — `[pytest.mark.integration, pytest.mark.regression]` — to add the
orthogonal `regression`/`slow` markers).

Directories (`tests/e2e/`, `tests/performance/`, `tests/contract/`) are a
convenience grouping only — a test runs under its marker wherever the file
sits. `tests/helpers/` holds shared helpers; never duplicate production schema
there.

Test fixtures import `INIT_SQL` and `MIGRATION_SQL` from `packages/estormi_server/sql/schema.py`
(via `tests/helpers/database.py`), so fixture databases always match production
startup — do not hand-copy schema into the test tree.

## Where Tests Live

The suite mirrors the source tree: each first-party package has a matching test
dir — `tests/estormi_server/`, `tests/estormi_ingestion/`,
`tests/estormi_briefing/`, `tests/estormi_distill/`, `tests/memory_core/`, and
`tests/connectors/` — one file per backend module or behaviour area
(`tests/estormi_server/test_tools.py`, `tests/estormi_server/test_pipeline.py`,
`tests/estormi_ingestion/test_ingestion.py`, …). Cross-cutting categories keep
their own dirs: `tests/e2e/`, `tests/contract/`, `tests/performance/`, and
`tests/tooling/` (build/CI/ops-script tests). Integration-marked tests live in
the mirror dir of the package they exercise (selected by the `integration`
marker, not a directory).
When adding coverage, find the file that already owns the
module under test and extend it rather than creating a new file; create a new
`test_<module>.py` only for a genuinely new module, and always give it a layer
marker.

`tests/contract/test_quality_contracts.py` is the contract layer — it asserts on docs,
Makefile, and CI-workflow content, so renaming a doc or workflow step will fail
here until the contract is updated. CI is split across `test.yml`, `rust.yml`,
`js.yml`, `security.yml`, and `release.yml`; the workflow contracts assert
required strings in each. See the **CI Workflows** section of the `infra`
skill before adding or moving jobs — the free-tier 2,000 minute/month budget
constrains where heavy jobs can live.

## Adding a Test (recipe)

1. **Home file.** Extend the existing file for the module under test in its package dir (`tests/estormi_server/`, `tests/estormi_ingestion/`, `tests/estormi_briefing/`, `tests/memory_core/`, `tests/connectors/`); create a new file only for a genuinely new module.
2. **Layer marker.** Module-level `pytestmark = pytest.mark.<layer>` (`unit` | `integration` | `e2e` | `contract` | `performance`). Use a list to stack orthogonal markers (e.g. `[pytest.mark.integration, pytest.mark.regression]`). A file with no layer marker runs in `make test` but is **skipped by every `make test-<layer>` target** — never leave one unmarked.
3. **Behaviour, not smoke.** Assert observable effects and cover failure branches, not just the happy path.
4. **Mock every external boundary.** Qdrant, keyring, subprocesses, the local LLM, the network — patch where the dependency is imported. Never hit a real service. Never write a large file or allocate a large object to "simulate" one (a runaway test can OOM the machine; `timeout = 60` catches it, but don't rely on that).
5. **Close what you open.** Every `aiosqlite` connection, subprocess, and task — a leaked background thread fails the run (warnings are errors).
6. **Verify.** `.venv/bin/pytest <file> -v`, then `make test` — coverage must stay above `--cov-fail-under`.

Full diagram of markers × targets × coverage in `docs/testing.md` ("Test architecture").

## Fixtures

### `db`

In-memory `aiosqlite.Connection` with the production Estormi schema applied by
`tests.helpers.database.apply_runtime_schema`.

```python
async def test_my_feature(db):
    await db.execute("INSERT INTO chunks (id, content_hash) VALUES (?, ?)", ("c1", "h1"))
    await db.commit()
```

### `db_on_disk`

File-backed SQLite DB for tests that need a real path.

### `mock_embedder`

Patches `estormi_server.storage.tools.embed_one` and `estormi_server.storage.tools.sparse_embed_one` with deterministic fake
vectors.

### `mock_qdrant`

Patches `estormi_server.storage.tools._client` with an `AsyncMock` that tracks Qdrant calls.

### `client`

`httpx.AsyncClient` using ASGI transport against the FastAPI app. The fixture
marks setup complete and mocks DB, embeddings, Qdrant, and keyring.

## Mocking Patterns

Patch where the dependency is imported, not where it originated:

```python
with patch("estormi_server.storage.chunk_admin.retag_chunks", new=AsyncMock(...)):
    result = await wa.set_chat_group_type(db, chat_id, group_type)
```

For the shared storage state, patch `estormi_server.storage.tools._client`,
`estormi_server.storage.tools.embed_one`, or `estormi_server.storage.tools.sparse_embed_one` directly —
the read/write paths reach them late-bound through `tools.<name>`.

Mock subprocesses with `AsyncMock` around `asyncio.create_subprocess_exec` and
provide the process attributes the route reads (`pid`, `returncode`, `wait`,
`stdout`, etc.).

Mock keyring in Settings tests:

```python
with patch("keyring.get_password", return_value=""), patch("keyring.set_password"):
    ...
```

## Common Pitfalls

| Pitfall | Fix |
|---|---|
| Patching `estormi_server.storage.tools.sqlite_conn` for a module that imported `sqlite_conn` directly | Patch `module.sqlite_conn` |
| DB writes not visible | Call `await db.commit()` |
| Test hits real Qdrant or model | Use fixtures or patch the module-level accessor |
| Setup redirect interferes with page tests | Use the `client` fixture's setup-complete default or delete `setup_completed` intentionally |

## Coverage Expectations

Coverage is generated by `make test` and gated by `--cov-fail-under` — the run
fails if total line coverage across the six measured roots (`estormi_server`,
`memory_core`, `connectors`, `estormi_ingestion`, `estormi_briefing`,
`estormi_distill`) drops below the floor.

Lower line coverage is still expected in individual modules that orchestrate
subprocesses, local models, launchd, Tauri/WhatsApp, or large HTML-rendered UI
pages. For risky changes in those areas, add focused behavior tests and, when
appropriate, run `make test-suite` on an installed machine.

The suite treats `PytestUnhandledThreadExceptionWarning` and
`PytestUnraisableExceptionWarning` as **errors** (see the
`[tool.pytest.ini_options]` table in `pyproject.toml`). If a test
opens an async resource (an `aiosqlite` connection, a subprocess, a task),
close it — a leaked background thread fails the run.

`pyproject.toml` also sets `timeout = 60` (via `pytest-timeout`): any test that
hangs — infinite loop, deadlock, runaway allocation — fails after 60s instead
of stalling the run. Never raise this to mask a slow test; fix the test.

To refresh the README badge after a coverage run:

```bash
.venv/bin/python scripts/qa_metrics.py build/coverage/coverage.json assets/badges
```
