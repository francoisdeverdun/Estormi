/**
 * EngineEventsBridge — subscribes to `/api/events` (SSE) and mirrors engine
 * lifecycle into the SystemStatus store.
 *
 * The backend emits `engine.snapshot` on connect, then a live stream of
 * `engine.started` / `engine.stopped` whenever any engine launches or
 * exits — including scheduled / cron starts that the SPA never triggered
 * itself. Without this bridge the LiveIndicator only knew about manual
 * starts, so a watcher-launched briefing stayed invisible until the user
 * changed page.
 *
 * The browser's EventSource auto-reconnects on transient drops; the
 * snapshot delivered on reconnect re-syncs the store without polling.
 */
import { useEffect, useRef } from 'react'
import {
  useSystemStatus,
  type EngineKind,
  type EngineStatus,
  type QueueEntry,
  type QueueSource,
} from './SystemStatus'

interface CurrentBlock {
  kind: EngineKind
  startedAt: number | null
}

interface LastBlock {
  kind: EngineKind
  status: EngineStatus
  endedAt: number | null
}

interface QueueWireEntry {
  kind: EngineKind
  source: QueueSource
  enqueuedAt: number
}

interface SnapshotEvent {
  type: 'engine.snapshot'
  current: CurrentBlock | null
  last: LastBlock | null
  queue?: QueueWireEntry[]
}

interface StartedEvent {
  type: 'engine.started'
  kind: EngineKind
  startedAt: number | null
}

interface StoppedEvent {
  type: 'engine.stopped'
  kind: EngineKind
  status: EngineStatus
  /** Server stop time (epoch seconds); the authoritative "Xs ago" anchor. */
  endedAt?: number
}

interface QueueChangedEvent {
  type: 'queue.changed'
  queue: QueueWireEntry[]
}

type ServerEvent = SnapshotEvent | StartedEvent | StoppedEvent | QueueChangedEvent

const normaliseQueue = (raw: QueueWireEntry[] | undefined): QueueEntry[] =>
  (raw ?? []).map((e) => ({
    kind: e.kind,
    source: e.source,
    enqueuedAt: e.enqueuedAt,
  }))

// Backend sends epoch seconds (Python `time.time()`); SystemStatus expects
// epoch milliseconds (`Date.now()`).
const toMs = (epochSec: number | null | undefined): number | undefined =>
  typeof epochSec === 'number' ? epochSec * 1000 : undefined

export function EngineEventsBridge() {
  const sys = useSystemStatus()

  // Capture callbacks through refs so the EventSource only opens once and
  // doesn't get torn down on every render.
  const startRef = useRef(sys.start)
  const stopRef = useRef(sys.stop)
  const setQueueRef = useRef(sys.setQueue)
  const setLastJobRef = useRef(sys.setLastJob)
  startRef.current = sys.start
  stopRef.current = sys.stop
  setQueueRef.current = sys.setQueue
  setLastJobRef.current = sys.setLastJob

  useEffect(() => {
    let es: EventSource | null = null
    let reopenTimer: number | null = null
    let cancelled = false

    const handle = (raw: MessageEvent) => {
      let data: ServerEvent
      try {
        data = JSON.parse(raw.data) as ServerEvent
      } catch {
        return // malformed payload — drop silently
      }
      if (data.type === 'engine.started') {
        startRef.current(data.kind, toMs(data.startedAt))
        return
      }
      if (data.type === 'engine.stopped') {
        stopRef.current(data.status ?? 'ok', data.kind, toMs(data.endedAt))
        return
      }
      if (data.type === 'queue.changed') {
        setQueueRef.current(normaliseQueue(data.queue))
        return
      }
      if (data.type === 'engine.snapshot') {
        if (data.current) {
          startRef.current(data.current.kind, toMs(data.current.startedAt))
        } else {
          // Server says nothing is running — reconcile any stale local state.
          stopRef.current('ok')
        }
        if (data.last && data.last.endedAt != null) {
          setLastJobRef.current({
            kind: data.last.kind,
            status: data.last.status,
            endedAt: data.last.endedAt * 1000,
          })
        }
        setQueueRef.current(normaliseQueue(data.queue))
      }
    }

    const open = () => {
      if (cancelled) return
      if (reopenTimer != null) {
        window.clearTimeout(reopenTimer)
        reopenTimer = null
      }
      es?.close()
      es = new EventSource('/api/events')
      es.addEventListener('engine.started', handle)
      es.addEventListener('engine.stopped', handle)
      es.addEventListener('engine.snapshot', handle)
      es.addEventListener('queue.changed', handle)
      es.onerror = () => {
        // Built-in retry handles transient drops. When it gives up
        // (readyState === CLOSED) we reopen ourselves so the bridge
        // survives long outages without leaving the UI stuck on stale
        // engine state.
        if (es && es.readyState === EventSource.CLOSED) {
          reopenTimer = window.setTimeout(open, 1500)
        }
      }
    }

    // After OS sleep the TCP socket can be half-open: the browser thinks
    // the EventSource is OPEN but no events flow. visibilitychange gives
    // us a deterministic moment to force-reopen so the snapshot on
    // reconnect reconciles whatever the engines did in the meantime.
    const onVisible = () => {
      if (document.visibilityState === 'visible') open()
    }

    open()
    document.addEventListener('visibilitychange', onVisible)

    return () => {
      cancelled = true
      document.removeEventListener('visibilitychange', onVisible)
      if (reopenTimer != null) window.clearTimeout(reopenTimer)
      es?.close()
    }
  }, [])

  return null
}
