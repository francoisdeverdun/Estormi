/**
 * Settings-overview API client.
 *
 * One endpoint returns the headline numbers the dashboard needs: chunk
 * count, model state, storage size, sources counts, pipeline summary. Used
 * by the Ingestion page hero strip.
 */
import { apiGet } from './client'

export interface OverviewModel {
  name: string
  loaded: boolean
  exists: boolean
  size_bytes: number
  tier?: string
}

export interface OverviewStorage {
  db_bytes: number
  qdrant_bytes: number
  staging_bytes: number
  /** WhatsApp durable message log ("cache") — a slice of db_bytes, broken out
   *  in the storage bar. May be absent on older snapshots. */
  whatsapp_cache_bytes?: number
  total_chunks: number
}

export interface OverviewSources {
  counts: Record<string, number>
  watermarks: Record<string, string>
}

export interface Overview {
  data_dir: string
  settings: Record<string, string>
  storage: OverviewStorage
  sources: OverviewSources
  model: OverviewModel
  pipeline: {
    next_run_at: string
    last_run_started: string
    overall_status: string
    /** Stage names (lowercased pipeline keys) that failed in the last run.
     *  SourcesPanel uses this to flip individual rows to the "Error" chip. */
    last_run_failed_stages?: string[]
  }
  /** WhatsApp bridge state. `connected` is the live link; `paired` is the
   *  sticky setup bit (true once the QR has been scanned, stays true between
   *  bounded nightly syncs even though the bot idles in between). */
  whatsapp?: { connected: boolean; paired?: boolean; session_state: string }
  /** macOS permission probes. `imessage_fda` is true/false once the Tauri
   *  shell has probed Full Disk Access, or null when not yet probed
   *  (e.g. running from source without the native shell). */
  permissions?: { imessage_fda: boolean | null }
}

export const getOverview = () => apiGet<Overview>('/api/settings/overview')
