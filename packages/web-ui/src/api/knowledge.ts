/**
 * Knowledge / Briefing API client.
 *
 * Backed by `estormi_server/api/knowledge.py`. The engine room dispatches a
 * briefing run through here; the briefing page reads the assembled
 * briefings plus chunks (filter source=briefing) to surface the most recent
 * synthesis. The live run log is tailed via the generic stage-log fetch in
 * `EngineRoomPopover`, so no typed log/status client is needed.
 */
import { apiGet, apiSend } from './client'

/** Enqueue (or start) a briefing composition run. */
export const runKnowledge = () => apiSend<unknown>('/api/knowledge/run', 'POST')

/**
 * Enqueue a health-only refresh of today's briefing (~1 minute): fresh WHOOP
 * pull, readiness card recomposed and spliced, audio re-narrated in the
 * background. Falls back to a full run when no briefing exists yet.
 */
export const refreshKnowledgeHealth = () =>
  apiSend<{ status: string }>('/api/knowledge/refresh-health', 'POST')

export interface BriefingDeleteResponse {
  deleted: number
  date: string
  vault: boolean
}

/** Fully delete one day's briefing — chunks + iCloud vault file. Idempotent. */
export const deleteBriefing = (date: string) =>
  apiSend<BriefingDeleteResponse>(`/api/briefings/${encodeURIComponent(date)}`, 'DELETE')

export interface BriefingsResetResponse {
  status: string
  chunks_deleted: number
  vault_files_deleted: number
}

/** Wipe every composed briefing plus the engine's run history. */
export const resetBriefings = () =>
  apiSend<BriefingsResetResponse>('/api/briefings/reset', 'POST')

export interface BriefingSummary {
  date: string
  title: string
  // Present on the wire but not currently rendered by the SPA.
  generatedAt?: string | null
  sourceCount?: number | null
  videoCount?: number | null
}

/**
 * Plain-text source of each editable prose section, present on briefings
 * composed after the field editor shipped. When present, the SPA offers a
 * structured form instead of the raw-HTML textarea; the server re-renders an
 * edited field and splices it between that section's zone markers in `htmlBody`.
 */
export interface BriefingFields {
  objective?: string
  readiness?: string
  myDay?: string
}

export interface Briefing extends BriefingSummary {
  id?: string
  htmlBody: string
  fields?: BriefingFields
}

/** List the assembled briefings on disk (newest first). */
export const listBriefings = () =>
  apiGet<{ items: BriefingSummary[] }>('/api/briefings')

/** Read one day's assembled briefing JSON (with htmlBody). */
export const getBriefing = (date: string) =>
  apiGet<Briefing>(`/api/briefings/${encodeURIComponent(date)}`)

/**
 * Save a user-edited briefing back to the vault. A human-corrected briefing is
 * the best possible distillation reference, so the edit is folded into the local
 * quill's training set (registered as a `user-edited` reference + exemplars) for
 * the next retrain. The save does not re-notify the iOS companion.
 */
export const editBriefing = (date: string, htmlBody: string) =>
  apiSend<{ date: string; saved: boolean }>(
    `/api/briefings/${encodeURIComponent(date)}`,
    'PUT',
    { htmlBody },
  )

/**
 * Save a structured (per-section) edit. Sends only the changed prose fields;
 * the server re-renders each and splices it into the stored HTML, leaving the
 * derived timeline / Around / World blocks untouched. Folds into the quill's
 * training set exactly like {@link editBriefing}.
 */
export const editBriefingFields = (date: string, fields: BriefingFields) =>
  apiSend<{ date: string; saved: boolean }>(
    `/api/briefings/${encodeURIComponent(date)}`,
    'PUT',
    { fields },
  )
