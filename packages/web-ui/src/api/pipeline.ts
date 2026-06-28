import { apiGet, apiSend } from './client'

/**
 * Shape returned by ``GET /api/pipeline`` — see
 * ``estormi_server/pipeline.py::get_pipeline_data``. All time fields are
 * human-formatted strings (``"—"`` when missing) so the UI can render
 * them as-is.
 */
export interface PipelineStage {
  name: string
  /** ``"ok" | "fail" | "skip" | "running" | "wait"`` */
  status: string
  duration_s: number | null
  duration: string
  /** Wall-clock start of this stage, epoch ms. Populated only for the
   *  currently-running stage so the SPA can grow its bar in real time
   *  between the 5 s pipeline polls. */
  started_at_epoch_ms?: number | null
}

export interface PipelineRun {
  started_at: string | null
  duration: string
  duration_s: number | null
  status: string
  failed_stages: string[]
  log_file: string | null
  /** Per-stage log paths for THIS run, keyed by canonical pipeline stage name
   *  (`notes`, `mail`, …). Empty when the parser hadn't captured one. Read by
   *  `sourcestable/dag.ts` to back the per-run log in SourceHistoryModal. */
  stage_logs?: Record<string, string>
  /** Per-stage final status for this run. Read by `sourcestable/dag.ts` to
   *  colour each source's per-run history cells. */
  stage_statuses?: Record<string, string>
  /** Per-stage pre-formatted duration ("3m 38s"). Backs SourceHistoryModal so
   *  it shows the stage's own duration instead of the full run's. */
  stage_durations?: Record<string, string>
  /** Per-stage duration in seconds. */
  stage_durations_s?: Record<string, number>
  /** Per-stage start offset from the run start, in seconds. */
  stage_offsets_s?: Record<string, number>
}

export interface PipelineData {
  is_running: boolean
  /** ``"ok" | "fail" | "running" | "unknown"`` */
  overall_status: string
  last_run_started: string | null
  last_run_ended: string | null
  last_run_duration_s: number | null
  last_run_duration: string
  last_run_ago: string
  last_run_failed_stages: string[]
  /** Total chunks ingested since the last run started. Approximation: rows in
   *  the chunks table with ``ingested_at >= last_run.started_at``. Runs are
   *  serialised by the engine mutex, so this attributes new chunks to the
   *  most recent run with negligible noise. */
  last_run_chunks_added?: number
  /** Same value, broken out per source key (``notes``, ``mail``, …). */
  last_run_chunks_by_source?: Record<string, number>
  mean_duration_s: number | null
  mean_duration: string
  next_run_at: string | null
  run_count: number
  errors: unknown[]
  stages: PipelineStage[]
  history: PipelineRun[]
}

export const getPipeline = () => apiGet<PipelineData>('/api/pipeline')
export const runPipeline = (stage?: string) =>
  apiSend<{ status: string; stage?: string }>(
    stage ? `/api/pipeline/run?stage=${encodeURIComponent(stage)}` : '/api/pipeline/run',
    'POST',
  )
export const stopPipeline = () => apiSend<{ status: string }>('/api/pipeline/stop', 'POST')
