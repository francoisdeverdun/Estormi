/**
 * EngineRoomPopover — the engine room popover lifted out of LiveIndicator.
 *
 * Same model: current job (with Stop) + queue (with per-row remove + Clear)
 * + engine launch grid (ingestion + briefing tiles; distill launches from its
 * own DistillationCard) — click a tile to enqueue that engine. In the one-pager the
 * trigger is the left engine pulse in OnePagerTopBar — not a duplicate
 * right-side badge — so this component owns only the popover content,
 * positioned and shown by the parent.
 */
import { useEffect, useRef, useState } from 'react'
import { Fleuron, Diamond } from '@estormi/ui-kit'
import {
  ENGINES,
  humanAgo,
  useSystemStatus,
  type EngineKind,
} from '../state/SystemStatus'
import { apiSend } from '../api/client'
import { LiveDot } from './engineroom/LiveDot'
import { QueueRow } from './engineroom/QueueRow'
import { EnginesGrid } from './engineroom/EnginesGrid'
import { EngineLogModal } from './engineroom/EngineLogModal'
import { UpcomingSection } from './engineroom/UpcomingSection'

export interface EngineRoomPopoverProps {
  /** Click outside / Escape calls this. */
  onClose: () => void
}

export function EngineRoomPopover({ onClose }: EngineRoomPopoverProps) {
  const sys = useSystemStatus()
  const ref = useRef<HTMLDivElement | null>(null)
  const [elapsed, setElapsed] = useState(0)
  const [logEngine, setLogEngine] = useState<EngineKind | null>(null)
  const [clearing, setClearing] = useState(false)

  const labelOf = (kind: EngineKind | null | undefined) =>
    kind ? ENGINES[kind].running : ''

  useEffect(() => {
    if (!sys.job || !sys.startedAt) {
      setElapsed(0)
      return
    }
    const tick = () => setElapsed(Math.floor((Date.now() - (sys.startedAt ?? 0)) / 1000))
    tick()
    const id = window.setInterval(tick, 1000)
    return () => window.clearInterval(id)
  }, [sys.job, sys.startedAt])

  useEffect(() => {
    // While the log modal is open it owns its own dismissal (scrim click +
    // Escape) and is portaled OUTSIDE this popover's ref, so clicks inside it
    // (stage pinning, raw-log toggle, text selection) would otherwise read as
    // "outside" and tear down the whole popover. Suspend the outside-click /
    // Escape handlers until the modal closes.
    if (logEngine) return
    const onDown = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) onClose()
    }
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose()
    }
    document.addEventListener('mousedown', onDown)
    document.addEventListener('keydown', onKey)
    return () => {
      document.removeEventListener('mousedown', onDown)
      document.removeEventListener('keydown', onKey)
    }
  }, [onClose, logEngine])

  const running = sys.job != null
  const meta = sys.job != null ? ENGINES[sys.job] : null
  const color = meta?.color ?? 'var(--vert-sauge)'
  const elapsedStr = `${String(Math.floor(elapsed / 60)).padStart(2, '0')}:${String(elapsed % 60).padStart(2, '0')}`
  const lastMeta = sys.lastJob ? ENGINES[sys.lastJob.kind] : null
  const sinceLast = sys.lastJob ? humanAgo(sys.lastJob.endedAt) : null
  const queueKinds = sys.queue.map((q) => q.kind)

  return (
    <>
      <div
        ref={ref}
        role="dialog"
        aria-label={"Engine room"}
        style={{
          position: 'absolute',
          top: 'calc(100% + 6px)',
          left: 0,
          minWidth: 320,
          maxWidth: 380,
          background: 'var(--charbon)',
          border: '1px solid var(--gilt-line-strong)',
          borderTop: `3px solid ${color}`,
          borderRadius: 'var(--radius-panel)',
          boxShadow: '0 12px 32px var(--shadow-faint)',
          padding: 12,
          zIndex: 120,
        }}
      >
        <div
          style={{
            fontFamily: 'var(--font-display)',
            fontSize: 10,
            letterSpacing: '0.28em',
            color: 'var(--or-ancien)',
            textTransform: 'uppercase',
            display: 'flex',
            alignItems: 'center',
            gap: 6,
            marginBottom: 10,
          }}
        >
          <Fleuron size={6} />{' '}
          {"Engine room"}
        </div>

        {/* Current job */}
        <div
          style={{
            display: 'flex',
            alignItems: 'stretch',
            marginBottom: 8,
            background: running
            ? `color-mix(in srgb, ${color} 10%, transparent)`
            : 'var(--well-dim)',
            border: `1px solid ${running ? color : 'var(--gilt-line)'}`,
            borderLeft: `3px solid ${running ? color : 'var(--gilt-line)'}`,
            borderRadius: 'var(--radius-tight)',
          }}
        >
          <button
            type="button"
            onClick={() => {
              const target = running ? sys.job : sys.lastJob?.kind ?? null
              if (target) setLogEngine(target)
            }}
            disabled={!running && !sys.lastJob}
            title={
              running
                ? `${labelOf(sys.job)} · click to view logs`
                : sys.lastJob
                  ? `${labelOf(sys.lastJob.kind)} · click to view last run logs`
                  : 'idle'
            }
            style={{
              flex: 1,
              padding: '9px 10px',
              background: 'transparent',
              border: 'none',
              display: 'flex',
              alignItems: 'center',
              gap: 10,
              textAlign: 'left',
              cursor: running || sys.lastJob ? 'pointer' : 'default',
              font: 'inherit',
              color: 'inherit',
              minWidth: 0,
            }}
          >
            <LiveDot running={running} color={color} size={9} />
            <div style={{ flex: 1, minWidth: 0 }}>
              <div
                style={{
                  fontFamily: 'var(--font-display)',
                  fontSize: 11,
                  letterSpacing: '0.18em',
                  color: running ? color : 'var(--ink-dim)',
                  textTransform: 'uppercase',
                  fontWeight: 700,
                }}
              >
                {running
                  ? labelOf(sys.job)
                  : sys.queue.length > 0
                    ? "Queued"
                    : "Idle"}
              </div>
              <div
                style={{
                  fontFamily: 'var(--font-mono)',
                  fontSize: 10,
                  color: 'var(--ink-dim)',
                  marginTop: 2,
                }}
              >
                {running
                  ? `${meta?.sub} · ${elapsedStr}`
                  : lastMeta
                    ? `last · ${lastMeta.label.toLowerCase()} ${sinceLast} (${sys.lastJob?.status})`
                    : 'no recorded work'}
              </div>
            </div>
          </button>
          {running && sys.job && (
            <button
              type="button"
              onClick={() => {
                const target = sys.job
                if (!target) return
                // Optimistic stop — flip the UI to "stopped" immediately so
                // the user sees the click landed. SSE delivers the real
                // engine.stopped within ms. We don't lock the button into a
                // "…" state because the optimistic switch already gives
                // direct feedback.
                const wasStartedAt = sys.startedAt ?? undefined
                sys.stop('cancelled', target)
                apiSend('/api/jobs/stop', 'POST', { kind: target }).catch(() => {
                  // The stop request failed, so the engine is almost certainly
                  // still running. The server emits no new started/snapshot
                  // (nothing changed), so SSE will NOT restore the badge — we
                  // must re-assert the running state locally, anchored to the
                  // original start time so the elapsed clock stays correct.
                  sys.start(target, wasStartedAt)
                })
              }}
              title={`Stop ${labelOf(sys.job)}`}
              aria-label={`Stop ${labelOf(sys.job)}`}
              style={{
                padding: '0 12px',
                background: 'transparent',
                border: 'none',
                borderLeft: `1px solid ${color}`,
                color: color,
                fontFamily: 'var(--font-mono)',
                fontSize: 13,
                lineHeight: 1,
                cursor: 'pointer',
              }}
            >
              ■
            </button>
          )}
        </div>

        {/* Queue */}
        <div style={{ marginBottom: 10 }}>
          <div
            style={{
              fontFamily: 'var(--font-display)',
              fontSize: 9,
              letterSpacing: '0.24em',
              color: 'var(--or-ancien)',
              textTransform: 'uppercase',
              marginBottom: 5,
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'space-between',
            }}
          >
            <span style={{ display: 'inline-flex', alignItems: 'center', gap: 5 }}>
              <Diamond size={5} color="var(--or-ancien)" /> Queue
            </span>
            <span style={{ display: 'inline-flex', alignItems: 'center', gap: 8 }}>
              <span
                style={{
                  fontFamily: 'var(--font-mono)',
                  color: 'var(--ink-dim)',
                  letterSpacing: 0,
                  fontSize: 10,
                }}
              >
                {sys.queue.length}
              </span>
              {sys.queue.length > 0 && (
                <button
                  type="button"
                  onClick={() => {
                    setClearing(true)
                    apiSend('/api/jobs/queue/clear', 'POST')
                      .catch(() => {})
                      .finally(() => setClearing(false))
                  }}
                  disabled={clearing}
                  style={{
                    padding: '1px 6px',
                    background: 'transparent',
                    border: '1px solid var(--gilt-line)',
                    color: 'var(--ink-dim)',
                    fontFamily: 'var(--font-display)',
                    fontSize: 8,
                    letterSpacing: '0.18em',
                    textTransform: 'uppercase',
                    cursor: clearing ? 'progress' : 'pointer',
                  }}
                >
                  {clearing ? 'Clearing…' : 'Clear'}
                </button>
              )}
            </span>
          </div>
          {sys.queue.length === 0 ? (
            <div
              style={{
                fontFamily: 'var(--font-ui)',
                fontSize: 11,
                color: 'var(--ink-dimmer)',
                fontStyle: 'italic',
                padding: '2px 0',
              }}
            >
              « nothing waiting »
            </div>
          ) : (
            <div style={{ display: 'flex', flexDirection: 'column', gap: 2 }}>
              {sys.queue.map((entry, i) => (
                <QueueRow
                  key={`${entry.kind}-${entry.enqueuedAt}-${i}`}
                  entry={entry}
                  position={i + 1}
                  onOpenLog={setLogEngine}
                />
              ))}
            </div>
          )}
        </div>

        {/* Upcoming automatic launches — daily crons + the WHOOP wake
            trigger (readiness refresh at wake). One-shot fetch per open. */}
        <UpcomingSection />

        {/* Engines grid — click to enqueue. Logs still reachable via the
            current/last job row at the top of this popover, and via queue
            entries (each row opens its engine's logs on click). */}
        <EnginesGrid
          queueKinds={queueKinds}
          currentJob={sys.job}
        />
        <p
          style={{
            fontFamily: 'var(--font-ui)',
            fontSize: 10,
            color: 'var(--ink-dimmer)',
            marginTop: 8,
            marginBottom: 0,
            lineHeight: 1.4,
            fontStyle: 'italic',
          }}
        >
          Click an engine to add it to the queue. Only one engine runs at a
          time — triggers stack and dispatch in order. The current run + its
          logs sit at the top of this panel.
        </p>
      </div>

      {logEngine && (
        <EngineLogModal kind={logEngine} onClose={() => setLogEngine(null)} />
      )}
    </>
  )
}
