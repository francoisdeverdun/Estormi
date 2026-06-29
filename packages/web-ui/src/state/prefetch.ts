/**
 * Warm the snapshot cache at app launch so the first render of the one-pager
 * shows real numbers instead of zeros while the per-section polls catch up.
 *
 * Fired once during the splash window from App.tsx. Fire-and-forget — any
 * single failure is silent; each section's own load() will retry on mount.
 *
 * Keys here MUST match the `useSnapshotState` keys used by the consumers,
 * otherwise the cached values won't be seen:
 *   - `pipeline`            → hooks/usePipeline.ts (Sources panel)
 *
 * The `overview` snapshot is seeded by App's own overview-fetch effect — no
 * need to duplicate the call here.
 */
import { writeSnapshot } from './snapshotCache'
import { getPipeline } from '../api/pipeline'

let started: Promise<void> | null = null

/** Fire every section's initial fetch once, in parallel. Idempotent:
 *  subsequent calls return the same promise. Resolves when every prefetch has
 *  either settled or failed — never rejects. Callers can `await` it to hold a
 *  splash open until the cache is warm. */
export function prefetchAll(): Promise<void> {
  if (started) return started

  const tasks: Array<Promise<unknown>> = [
    // Ingestion — pipeline status (consumed by Sources panel via usePipeline).
    getPipeline()
      .then((p) => writeSnapshot('pipeline', p))
      .catch(() => {}),
  ]

  started = Promise.allSettled(tasks).then(() => {})
  return started
}
