/**
 * useSourcePipeline — the DAG plumbing behind SourcesPanel.
 *
 * Owns the pipeline poll (via usePipeline), the optimistic per-stage "running"
 * flag, the 1 s live-duration heartbeat, and the run-only / stop-all actions.
 * Extracted from SourcesPanel.tsx so the panel body stays focused on layout.
 */
import { useEffect, useMemo, useState } from 'react'
import { usePipeline } from '../../hooks/usePipeline'

interface StageInfo {
  status: string
  duration_s: number | null
  duration: string
  started_at_epoch_ms?: number
}

export function useSourcePipeline() {
  const { data: pipeline, error: pipelineError, refresh, run, stop } = usePipeline()
  const [optimisticStage, setOptimisticStage] = useState<string | null>(null)
  const [stageActionError, setStageActionError] = useState<string | null>(null)

  // 1 s heartbeat while any stage is running — drives the live duration
  // shown on the running row between 5 s pipeline polls.
  const [, setLiveTick] = useState(0)
  useEffect(() => {
    const anyRunning = (pipeline?.stages ?? []).some((s) => s.status === 'running')
    if (!anyRunning && !pipeline?.is_running) return
    const id = window.setInterval(() => setLiveTick((t) => t + 1), 1000)
    return () => window.clearInterval(id)
  }, [pipeline])

  // Clear optimistic flags once the backend catches up, or after a safety timeout.
  useEffect(() => {
    if (!optimisticStage) return
    if (pipeline?.is_running) {
      setOptimisticStage(null)
      return
    }
    const id = window.setTimeout(() => setOptimisticStage(null), 6000)
    return () => window.clearTimeout(id)
  }, [optimisticStage, pipeline?.is_running])

  const stopAll = async () => {
    await stop()
    void refresh()
  }

  const runOnly = async (key: string) => {
    setOptimisticStage(key)
    try {
      await run(key)
      setStageActionError(null)
      void refresh()
    } catch (e) {
      setOptimisticStage(null)
      setStageActionError(e instanceof Error ? e.message : String(e))
    }
  }

  const stageByName = useMemo(() => {
    const m = new Map<string, StageInfo>()
    for (const s of pipeline?.stages ?? []) {
      m.set(s.name.toLowerCase(), {
        status: s.status,
        duration_s: s.duration_s,
        duration: s.duration,
        started_at_epoch_ms: (s as { started_at_epoch_ms?: number }).started_at_epoch_ms,
      })
    }
    return m
  }, [pipeline])

  return {
    pipeline,
    pipelineError,
    optimisticStage,
    stageActionError,
    stageByName,
    runOnly,
    stopAll,
  }
}
