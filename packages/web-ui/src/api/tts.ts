/**
 * Local TTS (voice) model catalog client — the voice counterpart to
 * ``api/model``. ``/api/tts/catalog`` lists the narration models and which are
 * downloaded; the Officina card renders install state + download/delete from it.
 * Downloads stream progress over the GET-only EventSource endpoint
 * ``/api/tts/download?key=…`` (opened in MaintenanceCard), so it isn't wrapped
 * here.
 */
import { apiGet, apiSend } from './client'

/** One narration model in the TTS catalog. */
export interface TtsModel {
  key: string
  label: string
  /** Family grouping for display, e.g. "Mistral". */
  family: string
  min_ram_gb: number
  /** MLX snapshot size on the mirror — drives the download estimate. */
  expected_bytes: number
  downloaded: boolean
  size_bytes: number
}

export interface TtsCatalog {
  models: TtsModel[]
  /** Key of the model the briefing narration currently uses. */
  selected: string
  /**
   * Narrator presets (Voxtral voices). The active choice is the
   * ``briefing_tts_voice`` setting; empty means "match the briefing language".
   */
  voices: string[]
}

export const getTtsCatalog = (signal?: AbortSignal) =>
  apiGet<TtsCatalog>('/api/tts/catalog', signal)

/** Delete the downloaded TTS snapshot to reclaim disk. */
export const deleteTtsModel = (key: string) =>
  apiSend<{ key: string; deleted: boolean }>('/api/tts/delete', 'POST', { key })
