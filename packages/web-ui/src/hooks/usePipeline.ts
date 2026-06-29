import { useCallback, useEffect, useState } from 'react'
import { getPipeline, runPipeline, stopPipeline, type PipelineData } from '../api/pipeline'
import { useSnapshotState } from '../state/snapshotCache'

/**
 * Read-only view of the shared ``pipeline`` snapshot — no poll of its own.
 *
 * ``SourcesPanel`` (always mounted) runs the single ``usePipeline`` poller and
 * writes the ``pipeline`` snapshot every 5 s. Components that only need to
 * *read* that data — e.g. the ingestion log modal's ``IngestionStageBody``,
 * which mounts transiently on top of the always-on panel — use this instead of
 * spinning up a second identical ``/api/pipeline`` poll.
 */
export function usePipelineSnapshot(): PipelineData | null {
  const [data] = useSnapshotState<PipelineData | null>('pipeline', null)
  return data
}

export function usePipeline(pollMs = 5000) {
  // Cached so re-opening a modal doesn't flash an empty pipeline until the
  // next 5s poll lands.
  const [data, setData] = useSnapshotState<PipelineData | null>('pipeline', null)
  const [error, setError] = useState<string | null>(null)

  const refresh = useCallback(async () => {
    try {
      setData(await getPipeline())
      setError(null)
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to fetch pipeline')
    }
  }, [setData])

  useEffect(() => {
    void refresh()
    const id = window.setInterval(refresh, pollMs)
    return () => window.clearInterval(id)
  }, [refresh, pollMs])

  return { data, error, refresh, run: runPipeline, stop: stopPipeline }
}
