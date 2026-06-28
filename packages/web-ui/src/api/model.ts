/**
 * Local-model catalog client.
 *
 * ``/api/model/catalog`` lists every model the app knows about, which are
 * downloaded, and which model the briefing engine currently uses. The
 * Maintenance card renders install state + the model picker from it. Downloads stream
 * progress over the EventSource endpoint ``/api/model/download?tier=…`` (see
 * MaintenanceCard) — that's GET-only, so it isn't wrapped here.
 */
import { apiGet, apiSend } from './client'

/** One model in the catalog. */
export interface CatalogModel {
  tier: string
  label: string
  /** Family grouping for display, e.g. "Mistral". */
  family: string
  min_ram_gb: number
  /** Q4_K_M GGUF size on the mirror — drives the download estimate. */
  expected_bytes: number
  downloaded: boolean
  size_bytes: number
}

/** The engine roles that carry their own model selection. */
export type EngineRole = 'briefing'

export interface ModelCatalog {
  models: CatalogModel[]
  /** Tier each engine currently resolves to (setting value, or its default). */
  selection: Record<EngineRole, string>
  /** Default tier per engine when its setting is unset. */
  defaults: Record<EngineRole, string>
}

export const getModelCatalog = (signal?: AbortSignal) =>
  apiGet<ModelCatalog>('/api/model/catalog', signal)

/** Delete a downloaded model's GGUF to reclaim disk. */
export const deleteModel = (tier: string) =>
  apiSend<{ tier: string; deleted: boolean }>('/api/model/delete', 'POST', { tier })
