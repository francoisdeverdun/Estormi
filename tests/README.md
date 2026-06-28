# tests

Pytest suite for Estormi.

## Directories

The bulk of the suite lives in **per-package mirror dirs** named after the
package under test:

- `estormi_server/`, `estormi_ingestion/`, `estormi_briefing/`,
  `estormi_distill/`, `memory_core/`, `connectors/` — tests mirroring the
  structure of each first-party package.

A few **cross-cutting dirs** hold tests that don't map onto a single package:

- `contract/` — docs, schema, Makefile, and CI-workflow contract checks.
- `e2e/` — end-to-end scenarios through the public HTTP/API surfaces.
- `performance/` — latency and throughput benchmarks with explicit thresholds.
- `tooling/` — tests for repo tooling (app bundle, QA metrics, security scan)
  that has no source-package mirror.
- `helpers/` — shared fixtures (`database.py`), not a test dir.

There is **no `unit/` or `integration/` dir** — `unit` and `integration` are
markers, not locations. (An older `integration/` dir was dissolved into the
mirror dirs; integration tests now live beside the package they exercise.)

**Where does a new test go?** Into the **mirror dir of the package under test**,
tagged with a `pytestmark` marker for its layer. CI selects by *marker*, not by
path (`make test-unit`, `make test-integration`, …), so the layer lives in the
marker and the home lives in the package.

## Markers

Markers are declared in `pyproject.toml` (`[tool.pytest.ini_options]`) and selected by the `make test-*` targets:
`unit`, `integration`, `e2e`, `regression`, `contract`, `slow`, `performance`.
Mark every test file with a `pytestmark` so the `make test-*` targets select it.

## Beyond pytest

- The real-Qdrant round-trip lives at `e2e/test_search_roundtrip_real.py` —
  it uses the real qdrant-client in embedded/local mode (no mock, no server)
  instead of the mocked boundary used elsewhere.
- iOS unit tests are the `EstormiTests` target (`apps/estormi-ios/Tests/`),
  run from Xcode; frontend tests (Vitest + Playwright) live in
  `packages/web-ui/`.
