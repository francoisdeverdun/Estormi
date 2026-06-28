/**
 * Settings API client — full surface used by the Settings page.
 *
 * Read endpoints (GET):
 *   /api/settings              flat key→value snapshot of the SQLite settings table
 *   /api/settings/overview     headline numbers + sources counts + mcp + model
 *   /api/knowledge/sources     YAML-backed knowledge sources list
 *
 * Write endpoints — these flip switches inside the FastAPI server.
 * State-changing ``/api/`` requests must carry an ``X-Estormi-Origin`` header;
 * ``apiSend`` in ``client.ts`` always sends it, and the origin gate (see
 * ``estormi_server/server/security.py``) rejects ``/api/`` writes without it.
 *
 * Contracts here mirror ``estormi_server/api/settings.py`` and
 * ``estormi_server/api/settings_ui.py``. Keep in sync with the Python side.
 */
import { apiGet, apiSend } from './client'
import type { components } from './schema'

/** Generated OpenAPI request-body schemas (from ``docs/specs/openapi.json``
 *  via ``openapi-typescript``). Typing the ``apiSend`` bodies against these
 *  wires the generated contract into the client so a drift between the SPA's
 *  request shape and the server's Pydantic model fails ``tsc`` instead of only
 *  surfacing at runtime. */
type Schemas = components['schemas']

export type Settings = Record<string, string>

export interface KnowledgeSource {
  id?: string
  label?: string
  type?: 'youtube_channel' | 'rss' | string
  url?: string
  urls?: string[]
  mode?: string
  axis?: string
  subtitle_langs?: string[]
  /** Optional per-source instruction prepended to the summarisation prompt. */
  pre_prompt?: string
  /** Whether the briefing engine should ingest this source. Missing /
   *  undefined is treated as enabled so existing YAML rows stay active. */
  enabled?: boolean
}

/** Server-side draft returned by /api/knowledge/resolve for a pasted URL. */
export interface ResolvedKnowledgeSource {
  type: 'youtube_channel' | 'rss'
  label: string
  url?: string
  urls?: string[]
  axis: string
}

// ── Reads ──────────────────────────────────────────────────────────────────

export const getSettings = () => apiGet<Settings>('/api/settings')

export const getKnowledgeSources = () =>
  apiGet<KnowledgeSource[]>('/api/knowledge/sources')

/** A selectable briefing-LLM model for the current provider. */
export interface LlmModelOption {
  value: string
  label: string
}

export interface KnowledgeLlmModels {
  models: LlmModelOption[]
  /** The model/tier currently in effect for this provider. */
  current: string
  /** Settings key the choice must be written to (provider-dependent). */
  setting_key: string
}

/** Models available for a briefing LLM provider, plus the settings key that
 *  stores the selection. `local` lists installed GGUFs; `claude-cli` lists the
 *  CLI's model aliases. */
export const getKnowledgeLlmModels = (provider: string) =>
  apiGet<KnowledgeLlmModels>(
    `/api/knowledge/llm-models?provider=${encodeURIComponent(provider)}`,
  )

// ── Writes ─────────────────────────────────────────────────────────────────

export const updateSettings = (patch: Settings) =>
  apiSend<Settings>('/api/settings', 'PUT', patch)

/** WHOOP "wake trigger" poller knobs. Mirrors the `whoop_polling_*` keys
 *  validated in `estormi_server/api/settings.py` and consumed by
 *  `jobs.apply_whoop_polling_schedule`. */
export interface WhoopPolling {
  enabled: boolean
  intervalMinutes: number
  windowStartHour: number
  windowEndHour: number
}

export const updateWhoopPolling = (p: WhoopPolling) =>
  updateSettings({
    whoop_polling_enabled: String(p.enabled),
    whoop_polling_interval_minutes: String(p.intervalMinutes),
    whoop_polling_window_start_hour: String(p.windowStartHour),
    whoop_polling_window_end_hour: String(p.windowEndHour),
  })

export const putKnowledgeSources = (sources: KnowledgeSource[]) =>
  apiSend<{ status: string; count: number }>(
    '/api/knowledge/sources',
    'PUT',
    sources,
  )

export const resolveKnowledgeSource = (url: string) =>
  apiSend<ResolvedKnowledgeSource>('/api/knowledge/resolve', 'POST', {
    url,
  } satisfies Schemas['_KbResolveBody'])

/**
 * Verified outcome of the macOS permission check Estormi runs when a
 * source is activated. `null` for sources that need no macOS permission.
 *
 *  - authorized   — access is granted; the source can ingest.
 *  - denied       — the user refused; grant it in System Settings.
 *  - manual       — macOS has no prompt (Full Disk Access); grant by hand.
 *  - undetermined — the prompt was shown but no answer was captured.
 *  - unavailable  — not macOS / frameworks absent — nothing to surface.
 */
export interface SourcePermission {
  key: string
  label: string
  status: 'authorized' | 'denied' | 'manual' | 'undetermined' | 'unavailable'
  detail: string
  /** System Settings deep link to fix a denial; null when nothing to do. */
  settings_pane: string | null
}

export const toggleSource = (name: string, enabled: boolean) =>
  apiSend<{ source: string; enabled: boolean; permission: SourcePermission | null }>(
    `/api/sources/${encodeURIComponent(name)}/toggle`,
    'POST',
    { enabled } satisfies Schemas['_ToggleSourceBody'],
  )

/**
 * Re-probe iMessage Full Disk Access so a grant the user just toggled in System
 * Settings is detected live, without relaunching Estormi. The running app/sidecar
 * can cache TCC's first decision; the backend re-checks from the FDA-holding main
 * binary (chat.db snapshot over the loopback). Used by the iMessage Manage modal's
 * Full Disk Access onboarding. See `estormi_server/api/permissions.py`.
 */
export const recheckFda = () =>
  apiSend<{ status: 'authorized' | 'manual' | 'unavailable' }>(
    '/api/permissions/recheck-fda',
    'POST',
    {},
  )

export const resetWatermark = (dbKey: string) =>
  apiSend<unknown>(
    `/api/sources/${encodeURIComponent(dbKey)}/watermark/reset`,
    'PUT',
  )

/**
 * Per-source Reset Data — drops chunks + Qdrant vectors + watermark for ONE
 * source, leaving every other source in the vault untouched. For WhatsApp this
 * is the *light* reset: the raw messages live in the durable `whatsapp_messages`
 * log, which is left intact, so the next run re-derives the chunks with no rescan.
 */
export const resetSourceData = (dbKey: string) =>
  apiSend<{ status: string; source: string; chunks_deleted: number; message: string }>(
    `/api/sources/${encodeURIComponent(dbKey)}/reset`,
    'POST',
  )

/**
 * Destructive WhatsApp-only reset: wipes the durable message log (the raw
 * captured messages) as well as the derived chunks. Unlike {@link resetSourceData},
 * the messages cannot be re-derived — the next run must RESCAN WhatsApp.
 */
export const resetWhatsAppLog = () =>
  apiSend<{ status: string; source: string; chunks_deleted: number; message: string }>(
    '/api/sources/whatsapp/log/reset',
    'POST',
  )
