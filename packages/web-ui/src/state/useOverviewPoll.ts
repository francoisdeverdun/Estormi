/**
 * Shared overview poller.
 *
 * Several sections (CardinalSection, MaintenanceCard, ParametersS/SourcesPanel)
 * all read the ``overview`` snapshot. Before this, two of them ran their own
 * ``getOverview`` timers (5s and 15s) — duplicate network traffic that wrote the
 * same snapshot key. This hook is a ref-counted singleton: no matter how many
 * components mount it, exactly ONE interval polls ``/api/settings/overview`` and
 * writes the ``overview`` snapshot; every ``useSnapshotState('overview')``
 * consumer re-renders off that single write.
 */
import { useEffect } from 'react'
import { getOverview } from '../api/overview'
import { writeSnapshot } from './snapshotCache'

const POLL_MS = 5_000
// A single dropped poll is usually a transient blip; keep the last-good
// snapshot through it. But a sustained outage left the snapshot frozen on a
// stale "connected" picture with no signal it was lying. After this many
// consecutive failures we blank the snapshot to ``null`` so consumers fall
// back to an honest "unknown" state instead of confidently showing stale data.
const FAIL_THRESHOLD = 3

let refCount = 0
let timer: number | null = null
let inFlight = false
let consecutiveFailures = 0

async function pollOnce(): Promise<void> {
  if (inFlight) return
  inFlight = true
  try {
    writeSnapshot('overview', await getOverview())
    consecutiveFailures = 0
  } catch {
    // Tolerate brief blips; only surface the outage once it's clearly
    // sustained, then let the next success restore the live snapshot.
    consecutiveFailures += 1
    if (consecutiveFailures >= FAIL_THRESHOLD) writeSnapshot('overview', null)
  } finally {
    inFlight = false
  }
}

export function useOverviewPoll(): void {
  useEffect(() => {
    refCount += 1
    if (timer === null) {
      void pollOnce()
      timer = window.setInterval(() => void pollOnce(), POLL_MS)
    }
    return () => {
      refCount -= 1
      if (refCount === 0 && timer !== null) {
        window.clearInterval(timer)
        timer = null
        consecutiveFailures = 0
      }
    }
  }, [])
}
