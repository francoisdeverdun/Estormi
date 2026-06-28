<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="assets/brand/estormi-wordmark-dark.svg">
    <img src="assets/brand/estormi-wordmark-light.svg" alt="Estormi" width="220">
  </picture>
</p>

<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="assets/brand/estormi-divider.svg">
    <img src="assets/brand/estormi-divider-light.svg" alt="" width="420">
  </picture>
</p>

# Security Policy

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="assets/brand/estormi-cap-E-dark.svg">
  <img src="assets/brand/estormi-cap-E-light.svg" align="left" width="56" height="54" hspace="6" vspace="2" alt="E">
</picture>

stormi is a local-first application: the database, search index, and
embeddings run on the user's own Mac. Security and privacy are core
to the product. The server is loopback-only by default; remote access requires
a bearer token, and the `security_boundary` middleware
(`packages/estormi_server/server/security.py`) enforces this on every non-public path.

## Network egress

Ingesting, indexing, and embedding are fully local — that data never leaves the
Mac. Network egress happens only in the places below, all under your control.

| Destination | What is sent | On by default? | Turn it off |
|---|---|:---:|---|
| **Briefing LLM** — Claude CLI (cloud), opt-in | The day's actions + your fresh memory for that day — only if you switch the Briefing provider to cloud | ⬚ off (the bundled local Ministral 3 composes by default — no egress) | Stays local unless you set `knowledge_llm_provider` to `claude-cli` |
| **Weather** — Open-Meteo (keyless) | Your home city + forecast lookup | ✅ for the configured location | Blank `briefing_home_location` (default `"Paris, France"`) |
| **Knowledge** — YouTube (`yt-dlp`) + RSS | Fetches transcripts/feeds you list | ⬚ none ship | Add no sources; RSS is SSRF-guarded (public hosts only) |
| **Cloud connectors** — Google Calendar, WHOOP | OAuth sync of your calendar/health | ⬚ only after you connect them | Don't connect them |
| **iCloud Drive vault** — Apple iCloud | Daily briefing, `metrics.json`, engine-run history | ✅ | Point `ESTORMI_VAULT_DIR` outside iCloud Drive (iOS companion then can't read it) |
| **APNs push** — `api.push.apple.com` | Content-free alert: briefing date only | ⬚ needs an APNs auth key | Don't configure an APNs key |
| **WhatsApp sidecar** — WhatsApp servers | WhatsApp Web protocol session | ⬚ only when WhatsApp is enabled | Don't enable WhatsApp |

Source references for the rows above: the Briefing engine
(`packages/estormi_briefing/`), the SSRF-guarded RSS fetch
(`packages/estormi_ingestion/knowledge/knowledge_fetch.py`), the vault writer
(`packages/estormi_ingestion/shared/delivery/vault_sync.py`), the APNs push
(`packages/estormi_ingestion/shared/delivery/apns_push.py`), and the WhatsApp bot
inside the Tauri shell (`apps/estormi-macos/src/whatsapp/`, bridging to a loopback
Axum endpoint on `127.0.0.1:9877`; continuous with `ESTORMI_WHATSAPP_ALWAYS_ON=true`).

**One-time model downloads** (not runtime telemetry):

- LLM GGUF (Ministral 3) streamed from `huggingface.co` when you trigger a model download in the Maintenance modal (`packages/memory_core/llm_local.py`).
- Voxtral TTS snapshot pulled via `huggingface_hub` on first use (`packages/memory_core/tts_local.py`).
- The fastembed embedder fetches its ONNX model on first use, then caches it (no network on later starts).
- `make bundle-python` fetches the `python-build-standalone` runtime archive from `github.com/astral-sh/python-build-standalone` (**build-time only**; never a runtime call from the packaged app).

**To run the briefing fully offline:** set the provider to `local`, blank
`briefing_home_location`, add no knowledge sources, and point
`ESTORMI_VAULT_DIR` at a folder outside iCloud Drive.

## Desktop shell hardening

The packaged macOS app (`apps/estormi-macos/`) ships a restrictive Content
Security Policy that tightens once the WebView leaves the splash for the SPA:

| Directive | Tauri splash (`tauri.conf.json`) | SPA at `/app/` (`_SPA_CSP`, `estormi_server/server/static.py`) |
|---|---|---|
| `default-src` / `script-src` | `'self' tauri: asset:` / `'self'` | `'self'` |
| `style-src` | `'self' 'unsafe-inline'` | `'self' 'unsafe-inline'` (inline design tokens only — never scripts) |
| `connect-src` | `http://127.0.0.1:8000` (reach local FastAPI) | `'self'` (same-origin localhost only) |
| `img-src` / `object-src` / `frame-ancestors` | — | `'self' data:` / `'none'` / `'none'` (also `base-uri 'self'`) |

On launch the WebView navigates to the FastAPI origin (`/app/`), after which the
Tauri policy is replaced entirely by the `_SPA_CSP` header, so no remote script
can load. The SPA uses `dangerouslySetInnerHTML` in exactly one place — the
briefing body (`packages/web-ui/src/sections/BriefingModal.tsx`) — and that
payload is assembled and HTML-escaped server-side by
`packages/estormi_briefing/compose/build_daily_note.py`, with `script-src 'self'`
+ `img-src 'self' data:` as a second layer that blocks any inline `<script>` or
remote image beacon. The engine run-queue
(`packages/estormi_server/server/jobs.py`) holds no remote surface: it only
builds the loopback URL (`http://127.0.0.1:{MCP_SERVER_PORT}`) and launches the
engine subprocesses. The sync `httpx` health/wake probe lives in
`packages/estormi_server/server/schedulers.py`, and the only direct-SQLite access
is the engine lock in `packages/memory_core/engine_lock.py` — both loopback/local
only.

## Supported Versions

Estormi follows a rolling-release model — only the latest tag is supported.

| | Supported |
|---|:---:|
| Latest released tag | ✅ |
| Any older tag | — |
| Platforms | macOS (Apple Silicon) + the iOS companion |

## Reporting a Vulnerability

**Please do not report security vulnerabilities through public GitHub issues.**

Instead, use GitHub's private vulnerability reporting:

1. Open the **Security** tab of this repository.
2. Click **Report a vulnerability**.
3. Describe the issue, the affected version, and reproduction steps.

We aim to acknowledge a report within 5 business days and to provide a remediation
timeline after triage. Please give us a reasonable window to release a fix before
any public disclosure.

## Scope

In scope:

- The FastAPI server and MCP transport (`packages/estormi_server/`).
- The storage and retrieval layer (`packages/memory_core/`).
- The native macOS shell (`apps/estormi-macos/`) — sidecar lifecycle, tray, WhatsApp
  embed (`apps/estormi-macos/src/whatsapp/`, Axum on `127.0.0.1:9877`).
- Ingestion connectors that handle untrusted file content
  (`packages/connectors/`, `packages/estormi_ingestion/`).

Out of scope:

- Vulnerabilities in third-party dependencies (report those upstream).
- Issues that require an attacker to already have local user-level access to the
  Mac, since Estormi's data is only as protected as the macOS account.
