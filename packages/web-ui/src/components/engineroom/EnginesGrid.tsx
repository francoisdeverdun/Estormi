/**
 * EnginesGrid — the launch grid at the foot of the engine-room popover. Renders
 * one tile per engine launchable from here (ingestion / briefing); distill is a
 * valid engine but launches from its own DistillationCard, so it has no tile.
 * Click an engine to enqueue it (optimistically), with rollback when the enqueue
 * call fails. Lifted out of EngineRoomPopover.tsx.
 */
import { useState } from 'react'
import { Diamond } from '@estormi/ui-kit'
import {
  ENGINES,
  useSystemStatus,
  type EngineKind,
  type EngineMeta,
  type QueueEntry,
} from '../../state/SystemStatus'
import { RUN_ENGINE } from './shared'

export function EnginesGrid({
  queueKinds,
  currentJob,
}: {
  queueKinds: EngineKind[]
  currentJob: EngineKind | null
}) {
  const sys = useSystemStatus()
  const [error, setError] = useState<string | null>(null)

  const enqueue = (kind: EngineKind) => {
    setError(null)
    // Optimistic enqueue — push the entry to the local queue *now* so the
    // user sees the click land immediately. On success SSE reconciles within
    // ms (`queue.changed` replaces the whole queue). On failure we must roll
    // back ourselves: a server-side rejection (e.g. already-running) or a
    // network error emits no `queue.changed`, so the optimistic entry would
    // otherwise linger until some unrelated later event.
    const optimistic: QueueEntry = {
      kind,
      source: 'manual',
      enqueuedAt: Math.floor(Date.now() / 1000),
    }
    sys.setQueue((q) => [...q, optimistic])
    // Non-null: the grid only renders tiles for kinds present in RUN_ENGINE.
    RUN_ENGINE[kind]!().catch((e) => {
      setError(e instanceof Error ? e.message : String(e))
      // Roll back the optimistic entry so the queue display matches the
      // server. Use a functional update so we splice from the *current*
      // queue, not the stale snapshot this closure captured at enqueue time
      // (SSE may have replaced it in between). Drop the exact entry we
      // pushed, falling back to the most recent matching kind.
      sys.setQueue((q) => {
        const idx = q.indexOf(optimistic)
        const at = idx >= 0 ? idx : q.findLastIndex((e) => e.kind === kind)
        if (at < 0) return q
        const next = [...q]
        next.splice(at, 1)
        return next
      })
      window.setTimeout(() => setError(null), 4000)
    })
  }

  return (
    <div>
      <div
        style={{
          fontFamily: 'var(--font-display)',
          fontSize: 9,
          letterSpacing: '0.24em',
          color: 'var(--or-ancien)',
          textTransform: 'uppercase',
          marginBottom: 5,
          display: 'inline-flex',
          alignItems: 'center',
          gap: 5,
        }}
      >
        <Diamond size={5} color="var(--or-ancien)" /> Engines
      </div>
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(2, 1fr)', gap: 3 }}>
        {(Object.entries(ENGINES) as Array<[EngineKind, EngineMeta]>)
          .filter(([k]) => k in RUN_ENGINE)
          .map(([k, m]) => {
            const isCurrent = currentJob === k
            const isQueued = queueKinds.includes(k)
            const disabled = isCurrent || isQueued
            const title = isCurrent
              ? `${m.label} · already running`
              : isQueued
                ? `${m.label} · already queued`
                : `${m.label} · click to add to queue`
            return (
              <button
                type="button"
                key={k}
                onClick={() => enqueue(k)}
                disabled={disabled}
                title={title}
                style={{
                  display: 'flex',
                  alignItems: 'center',
                  gap: 5,
                  padding: '5px 7px',
                  textAlign: 'left',
                  background: isCurrent
                    ? `color-mix(in srgb, ${m.color} 13%, transparent)`
                    : isQueued
                      ? 'rgba(200,164,103,0.08)'
                      : 'transparent',
                  border: `1px solid ${isCurrent ? m.color : isQueued ? 'var(--or-ancien)' : 'var(--gilt-line)'}`,
                  color: 'var(--parchemin)',
                  cursor: disabled ? 'not-allowed' : 'pointer',
                }}
              >
                <span
                  style={{
                    width: 5,
                    height: 5,
                    background: m.color,
                    borderRadius: '50%',
                    flexShrink: 0,
                    opacity: isCurrent ? 1 : 0.55,
                  }}
                />
                <span
                  style={{
                    fontFamily: 'var(--font-display)',
                    fontSize: 10,
                    letterSpacing: '0.14em',
                    textTransform: 'uppercase',
                    flex: 1,
                  }}
                >
                  {m.label}
                </span>
                {/* Right-side affordance hint: ● running · ⋯ queued · + queueable */}
                {isCurrent ? (
                  <span
                    style={{
                      fontFamily: 'var(--font-mono)',
                      fontSize: 10,
                      color: m.color,
                    }}
                  >
                    ●
                  </span>
                ) : isQueued ? (
                  <span
                    style={{
                      fontFamily: 'var(--font-mono)',
                      fontSize: 10,
                      color: 'var(--or-ancien)',
                    }}
                  >
                    ⋯
                  </span>
                ) : (
                  <span
                    aria-hidden="true"
                    style={{
                      fontFamily: 'var(--font-mono)',
                      fontSize: 11,
                      color: 'var(--ink-dim)',
                    }}
                  >
                    +
                  </span>
                )}
              </button>
            )
          },
        )}
      </div>
      {error && (
        <div
          style={{
            marginTop: 6,
            padding: '4px 8px',
            border: '1px solid var(--rouge-clair)',
            color: 'var(--rouge-clair)',
            fontFamily: 'var(--font-mono)',
            fontSize: 10,
          }}
        >
          {error}
        </div>
      )}
    </div>
  )
}
