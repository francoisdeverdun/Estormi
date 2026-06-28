/**
 * SystemStatusProvider — global "what's running right now".
 *
 * Estormi runs three engines (ingestion / briefing + the optional,
 * off-daily-path distill) and they never run in parallel — by design, to spare
 * compute and memory on the user's Mac. This store models that single-track queue.
 *
 * Source of truth lives on the server: `estormi_server/server/jobs._queue` is
 * the FIFO every launch path funnels through. The SSE stream
 * (`state/EngineEventsBridge.tsx`) writes `queue` here on every
 * `queue.changed` and on the initial `engine.snapshot`. Pages still call
 * `sys.start(...)` optimistically when they kick off a run so the badge
 * updates without waiting for the round trip; SSE then reconciles.
 */
import { createContext, useCallback, useContext, useMemo, useRef, useState } from 'react'
import type { Dispatch, ReactNode, SetStateAction } from 'react'

export type EngineKind = 'ingestion' | 'briefing' | 'distill'

export type EngineStatus = 'ok' | 'failed' | 'cancelled'

export type QueueSource = 'manual' | 'schedule' | 'backlog'

export interface QueueEntry {
  kind: EngineKind
  source: QueueSource
  enqueuedAt: number // epoch seconds (matches the SSE wire format)
}

export interface EngineMeta {
  /** Display name for titles, queue rows and log headers. Present-participle
   *  for ingestion; the noun "Briefing" reads better than "Composing" outside
   *  the live indicator. */
  label: string
  /** Present-participle state shown while the engine is the live job
   *  ("Composing" for briefing). */
  running: string
  sub: string
  color: string
}

export const ENGINES: Record<EngineKind, EngineMeta> = {
  ingestion: {
    label: 'Ingesting',
    running: 'Ingesting',
    sub: 'DAG · sources → vault',
    color: 'var(--pourpre-clair)',
  },
  briefing: {
    label: 'Briefing',
    running: 'Composing',
    sub: "composing today's briefing",
    color: 'var(--vert-sauge)',
  },
  // Optional third engine (Apple-Silicon only), off the daily path — but it runs
  // through the same single-track queue, so the badge / queue / log must be able
  // to render it. Its run button lives on the DistillationCard, not the engine
  // grid, so it is deliberately absent from RUN_ENGINE (see engineroom/shared.ts).
  distill: {
    label: 'Distill',
    running: 'Distilling',
    sub: 'retraining the local prose model',
    color: 'var(--or-ancien)',
  },
}

interface LastJob {
  kind: EngineKind
  endedAt: number
  status: EngineStatus
}

interface SystemStatusValue {
  job: EngineKind | null
  queue: QueueEntry[]
  lastJob: LastJob | null
  startedAt: number | null
  start: (kind: EngineKind, startedAt?: number) => void
  stop: (status?: EngineStatus, kind?: EngineKind, endedAt?: number) => void
  setQueue: Dispatch<SetStateAction<QueueEntry[]>>
  setLastJob: (last: LastJob | null) => void
}

const SystemStatusContext = createContext<SystemStatusValue>({
  job: null,
  queue: [],
  lastJob: null,
  startedAt: null,
  start: () => {},
  stop: () => {},
  setQueue: () => {},
  setLastJob: () => {},
})

export function SystemStatusProvider({ children }: { children: ReactNode }) {
  const [job, setJob] = useState<EngineKind | null>(null)
  const [queue, setQueue] = useState<QueueEntry[]>([])
  const [lastJob, setLastJob] = useState<LastJob | null>(null)
  const [startedAt, setStartedAt] = useState<number | null>(null)

  // Ref for synchronous reads inside callbacks — avoid stale closure when
  // stop() races with start() during the same render.
  const jobRef = useRef<EngineKind | null>(null)
  jobRef.current = job

  // `startedAt` lets a caller anchor the elapsed clock to a real backend
  // launch time (epoch ms). Pages that mirror server state pass it so the
  // LiveIndicator shows true run age; callers that trigger an engine
  // themselves omit it and the clock starts now.
  const start = useCallback((kind: EngineKind, startedAt?: number) => {
    if (jobRef.current === kind) {
      // Same engine already tracked — accept a more authoritative startedAt
      // from the server snapshot, but don't flicker the badge.
      if (startedAt) setStartedAt(startedAt)
      return
    }
    // The server's queue runner only ever has one engine in flight at a time,
    // so a `started` for a different kind while a job is tracked means the
    // prior one already finished — accept the new one unconditionally.
    setStartedAt(startedAt ?? Date.now())
    setJob(kind)
  }, [])

  // `endedAt` (epoch ms) lets a caller honour the server's authoritative stop
  // time; callers that observe the stop locally omit it and we stamp now.
  const stop = useCallback((status: EngineStatus = 'ok', kind?: EngineKind, endedAt?: number) => {
    const current = jobRef.current
    if (!current) return
    // A kind-targeted stop (from an SSE engine.stopped event) must not clear
    // a different engine that the store has since switched to.
    if (kind && current !== kind) return
    setLastJob({ kind: current, endedAt: endedAt ?? Date.now(), status })
    setStartedAt(null)
    setJob(null)
  }, [])

  const value = useMemo<SystemStatusValue>(
    () => ({ job, queue, lastJob, startedAt, start, stop, setQueue, setLastJob }),
    [job, queue, lastJob, startedAt, start, stop],
  )

  return <SystemStatusContext.Provider value={value}>{children}</SystemStatusContext.Provider>
}

export function useSystemStatus(): SystemStatusValue {
  return useContext(SystemStatusContext)
}

/** Human-readable "Xs/Xm/Xh/Xd ago" from a timestamp. */
export function humanAgo(ts: number): string {
  const s = Math.max(0, Math.floor((Date.now() - ts) / 1000))
  if (s < 60) return `${s}s ago`
  if (s < 3600) return `${Math.floor(s / 60)}m ago`
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`
  return `${Math.floor(s / 86400)}d ago`
}
