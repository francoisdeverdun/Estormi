/**
 * Distillation engine client.
 *
 * ``/api/distill/status`` is the whole contract: the engine writes
 * ``distill/status.json`` at every phase boundary and the card polls this
 * read-only view (workspace references, tooling probe, last verdict).
 * ``/api/distill/run`` enqueues one chain; progress then shows up both here
 * (phase) and in the engine room (the ``distill`` engine).
 */
import { apiGet, apiSend } from './client'

export interface DistillTooling {
  python: string
  mlx_lm: string
  quantize: string
  convert: string
  ready: boolean
}

export interface DistillStatus {
  status: {
    phase?: string
    error?: string
    refs?: { have: number }
    dataset?: { train: number; valid: number; days: number; models?: Record<string, number> }
    training?: { iters: number; valLosses: [number, number][]; chosenIter?: number | null; attempts?: number }
    verdict?: {
      pass: boolean
      baseClean: number
      tunedClean: number
      prompts: number
      artifact?: 'gguf' | 'adapter'
      stages?: Record<string, { prompts: number; baseClean: number; tunedClean: number }>
    } | null
    installed?: string
    lastTrainedAt?: string
    updatedAt?: string
  }
  references: {
    days: string[]
    count: number
    vaultCount?: number
    /** Minimum vault briefings before distillation is allowed (server-defined). */
    minBriefings?: number
    models: Record<string, number>
  }
  tooling: DistillTooling
  installed: boolean
  installedFile: string
  /** The distillation workspace dir + free space on its volume (needs ≥ needGb). */
  workspace?: { dir: string; freeGb: number | null; needGb: number }
  running: { kind: string; source: string }[]
}

export const getDistillStatus = (signal?: AbortSignal) =>
  apiGet<DistillStatus>('/api/distill/status', signal)

export const runDistill = () => apiSend<{ status: string }>('/api/distill/run', 'POST', {})

/** EventSource (GET) URL that streams the MLX toolchain install progress. */
export const distillToolingInstallPath = () => '/api/distill/tooling/install'

/** Remove the installed MLX training toolchain (venv + base cache) to reclaim disk. */
export const deleteDistillTooling = () =>
  apiSend<{ removed: boolean }>('/api/distill/tooling/delete', 'POST', {})
