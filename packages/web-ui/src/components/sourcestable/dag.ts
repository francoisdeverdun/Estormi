/**
 * DAG helpers for SourcesPanel — lifted from the (now-deleted) DagSection and
 * extracted out of SourcesPanel.tsx so the panel body stays focused on
 * rendering. Pure functions: run-status normalisation, per-source history
 * derivation, and the folder-root "awaiting setup" check.
 */
import type {
  SourceHistoryEntry,
  SourceRunStatus,
} from '../SourceRow'
import type { PipelineRun } from '../../api/pipeline'

/** The subset of a pipeline stage `liveStageView` reads — kept structural so
 *  it accepts both `PipelineStage` and the panel's lighter `StageInfo`. */
interface StageView {
  status: string
  duration: string
  started_at_epoch_ms?: number | null
}

const ROOT_REQUIRED_STAGES = new Set(['documents', 'code'])

export function isAwaitingSetup(
  key: string,
  settings: Record<string, string>,
): boolean {
  const lc = key.toLowerCase()
  if (!ROOT_REQUIRED_STAGES.has(lc)) return false
  const root = settings[`${lc}_root`]
  return !root || !String(root).trim()
}

export function normaliseRunStatus(s: string | undefined): SourceRunStatus | null {
  if (!s) return null
  const x = s.toLowerCase()
  if (x === 'ok' || x === 'success' || x === 'complete' || x === 'completed') return 'ok'
  if (x === 'fail' || x === 'failed' || x === 'error') return 'fail'
  if (x === 'running') return 'running'
  if (x === 'pending' || x === 'queued' || x === 'wait') return 'wait'
  if (x === 'skip' || x === 'skipped') return 'skip'
  if (x === 'cancelled' || x === 'canceled') return 'cancelled'
  return null
}

/**
 * Derive a source's live run status + display duration from its pipeline stage.
 *
 * The row list and the history-modal opener both need the same thing: an
 * optimistic ``running`` while a just-launched run hasn't surfaced in the poll
 * yet, otherwise the normalised stage status (defaulting to ``idle``); and a
 * duration that ticks up live as ``mm:ss`` while running, else the stage's own
 * pre-formatted duration. Returned `duration` is undefined when there's nothing
 * meaningful to show.
 */
export function liveStageView(
  stage: StageView | undefined,
  optimistic: boolean,
): { status: SourceRunStatus; duration: string | undefined } {
  const status: SourceRunStatus = optimistic
    ? 'running'
    : (normaliseRunStatus(stage?.status) ?? 'idle')
  let duration: string | undefined =
    stage?.duration && stage.duration !== '—' ? stage.duration : undefined
  if (status === 'running' && stage?.started_at_epoch_ms) {
    const secs = Math.max(0, Math.floor((Date.now() - stage.started_at_epoch_ms) / 1000))
    duration = `${String(Math.floor(secs / 60)).padStart(2, '0')}:${String(secs % 60).padStart(2, '0')}`
  }
  return { status, duration }
}

export function sourceHistory(
  key: string,
  runs: PipelineRun[],
  isRunning: boolean,
): SourceHistoryEntry[] {
  if (runs.length === 0) return []
  const lc = key.toLowerCase()
  // When NOT running, runs[0] is the most recent completed run; the
  // SourceRow already paints it as the live status pill, so skip it here.
  const startIdx = isRunning ? 0 : 1
  const entries: SourceHistoryEntry[] = []
  for (let i = startIdx; i < runs.length; i++) {
    const run = runs[i]
    const statuses = run.stage_statuses ?? {}
    const matched = Object.entries(statuses).find(
      ([k]) => k.toLowerCase() === lc,
    )?.[1]
    // Per-stage duration drives the bar height in the history strip.
    // Fall back to the run-level duration when the per-stage value is
    // missing (older snapshots).
    const durationS =
      (run.stage_durations_s?.[key] ??
        run.stage_durations_s?.[lc] ??
        undefined)
    const duration =
      run.stage_durations?.[key] ??
      run.stage_durations?.[lc] ??
      run.duration ??
      undefined
    if (matched !== undefined) {
      const s = matched.toLowerCase()
      const failed = s === 'fail' || s === 'failed' || s === 'error'
      const succeeded =
        s === 'ok' || s === 'success' || s === 'complete' || s === 'completed'
      if (!failed && !succeeded) continue
      entries.push({
        ok: !failed,
        status: matched,
        historyIdx: i,
        duration,
        durationS,
      })
      continue
    }
    const stageLogs = run.stage_logs ?? {}
    const haveLog = Object.keys(stageLogs).some((k) => k.toLowerCase() === lc)
    const failedNames = (run.failed_stages ?? []).map((s) => s.toLowerCase())
    const failedHere = failedNames.includes(lc)
    if (!haveLog && !failedHere) continue
    entries.push({
      ok: !failedHere,
      status: failedHere ? 'fail' : 'ok',
      historyIdx: i,
      duration,
      durationS,
    })
  }
  return entries.reverse()
}
