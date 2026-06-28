# Changelog

All notable changes to Estormi are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and the macOS app version (declared in `pyproject.toml`,
`apps/estormi-macos/Cargo.toml`, `apps/estormi-macos/tauri.conf.json`, and
`packages/estormi_server/__init__.py`) follows [Semantic
Versioning](https://semver.org/spec/v2.0.0.html). A contract test keeps those
four declarations in sync (`tests/contract/test_version_consistency.py`).

## [Unreleased]

### Added
- **Demo mode** with a fully fictional sample dataset: set `VITE_DEMO_MODE=true`
  for the web SPA, or tap **Explore a sample** in the iOS companion, to see every
  panel populated without a real vault.
- **Batch ingest endpoint** (`POST /api/ingest/batch`), used by WhatsApp
  ingestion to post many chunks in one request.
- **Wall-clock budget** for the briefing pipeline: a composition that overruns
  its time budget ships the best partial briefing instead of failing outright.

### Changed
- Logging across the briefing and ingestion packages is unified on `structlog`.

### Security
- **MCP bearer-token authentication.** `/mcp` and `/sse` are always protected by
  a per-launch random token; remote access requires the bearer.
- Hardened the HTTP boundary: trusted host-header check (defeats DNS rebinding),
  request body-size limit, audit-logged delete operations, and sanitized OAuth
  error responses.

### Fixed
- **Briefing readiness** stops hallucinating WHOOP figures; HTML fields stay in
  sync with the rendered output.
- **Correlation** drops closed and stale items from actionable threads instead
  of fusing months-old settled episodes into today's plan.
- **Voice and tone** enforcement: surgical sobriety pass strips coach-speak
  filler and melodrama; anti-fusion writer prompt prevents world-news content
  from leaking into personal narrative threads.
- **Vault listing** now only counts `YYYY-MM-DD.json` files as briefings,
  ignoring stray `.json` that previously caused phantom entries and
  manifest/delete errors.

## [0.0.3] - 2026-06-28

### Fixed
- **macOS app no longer hangs on the splash** when the backend's cold start is
  delayed past ~30s — e.g. behind the macOS *removable-volume* (TCC) prompt that
  appears when the data directory is relocated to an external disk. The shell's
  startup health poll that redirects the WebView off the bundled splash is now
  unbounded instead of giving up after 100 attempts, so the app always lands on
  the UI once the sidecar answers.

## [0.0.2] - 2026-06-21

### Added
- **Distributable CloudKit "doorbell" push.** Build tooling for the Developer ID
  + Production push helper, a signed helper embedded in the app bundle and
  auto-installed on first run, installed at the config home so it survives
  library moves, with a `codesign --verify` trust check before use.
- **`make ios-testflight`** — archive the iOS companion and upload it to
  TestFlight in one target.
- One download for both prose quills + the narration voice; the distillation
  engine is gated until at least five briefings exist.
- `.editorconfig` and `.gitattributes` codifying indent/charset/EOL policy and
  `linguist-generated` marking across the Python/TypeScript/Rust/Swift tree.
- This `CHANGELOG.md` (Keep a Changelog format), plus minimal `pyproject.toml` +
  orientation `README.md` files for the remaining first-party Python packages
  (`estormi_server`, `estormi_ingestion`, `estormi_briefing`, `estormi_distill`).
- `make set-version V=X.Y.Z` — single-source the macOS app version from one
  command — and `make audit-deps` — run `pip-audit` locally against the shipped
  bundle pins (`requirements/requirements-bundle.txt`).
- Golden-snapshot contract tests for the two cross-process contracts not covered
  by OpenAPI: the MCP tool registry (`mcp_rpc.py`, with a dispatcher-sync check
  that every advertised tool has a handler and vice versa) and the
  `briefings/<date>.json` vault payload the iOS companion reads.
- A pnpm `catalog:` centralizing the React/TypeScript/vitest versions shared by
  `@estormi/web-ui` and `@estormi/ui-kit`.
- Web-UI source-state clarity: Google Calendar shows its last sync-token save
  date, and iMessage shows a confirmed "Full Disk Access granted" state (and how
  to grant it when it is missing).

### Changed
- **Assistant-voice briefing narration** with softened Voxtral section-onset
  babble.
- "About you" moved to a Character modal on Summarium.
- Retired the OpenRouteService travel-transitions enrichment; weather is now
  keyless (Open-Meteo).
- Coverage JSON output relocated from the repo root to `build/coverage/` to keep
  the working-tree root clean.
- `pyright` type-checking extended from 2 to 4 first-party packages (added
  `connectors` + `estormi_distill`; `estormi_ingestion` + `estormi_briefing` are
  the documented next rungs).

### Fixed
- Roll back `write_txn` on a raised commit; route `retag_chunks` through it.
- Inclusive all-day Google Calendar end date; cap the world-corpus look-ahead.
- Store date-only reminders as a bare local date instead of `22:00Z` the day
  before, which had dated them a day early.
- Stamp the Google Calendar freshness watermark on a clean sync (not on a token
  advance); the WhatsApp backfill marker self-heals; unreadable PDFs no longer
  pin the documents ingest watermark forever.
- Distillation A/B-tests against the installed quill and reconciles orphaned
  runs.
- Make `Estormi.entitlements` AMFI-parseable (drop the XML comment).

### Security
- Pin model downloads to an immutable Hugging Face revision and verify the
  SHA-256.
- Hash-pin every bundled wheel and bump `yt-dlp` past four CVEs.
- Extend the CSRF origin gate to the `/ingest_chunk` and `/ingest_delete` shims.
- Bumped vulnerable bundle pins surfaced by `make audit-deps`:
  `cryptography` 48.0.0 → 48.0.1 (GHSA-537c-gmf6-5ccf) and `starlette` 1.2.1 →
  1.3.1 (GHSA-82w8-qh3p-5jfq, GHSA-jp82-jpqv-5vv3). `diskcache`
  GHSA-w8v5-vhqr-4h9v has no fixed release yet — tracked via an `--ignore-vuln`
  in `make audit-deps` to revisit when one ships.

## [0.0.1]

Baseline release at which this changelog begins. Estormi ships as a macOS Tauri
app (`Estormi.app`) — distributed directly, so the published DMG is unsigned and
not Apple-notarized — plus a read-only native iOS companion; see
[`docs/release.md`](docs/release.md) for the build and packaging flow.

[Unreleased]: https://github.com/francoisdeverdun/Estormi/compare/v0.0.3...HEAD
[0.0.3]: https://github.com/francoisdeverdun/Estormi/compare/v0.0.2...v0.0.3
[0.0.2]: https://github.com/francoisdeverdun/Estormi/compare/v0.0.1...v0.0.2
[0.0.1]: https://github.com/francoisdeverdun/Estormi/releases/tag/v0.0.1
