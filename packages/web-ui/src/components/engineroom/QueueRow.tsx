/**
 * QueueRow — a single waiting-engine row in the engine-room popover queue.
 *
 * Lifted out of EngineRoomPopover.tsx. Opens its engine's logs on click and
 * carries a per-row remove (×) with an optimistic-timeout/error affordance.
 */
import { useState } from 'react'
import { ENGINES, type EngineKind, type QueueEntry } from '../../state/SystemStatus'
import { apiSend } from '../../api/client'
import { QUEUE_SOURCE_LABEL } from './shared'

export function QueueRow({
  entry,
  position,
  onOpenLog,
}: {
  entry: QueueEntry
  position: number
  onOpenLog: (kind: EngineKind) => void
}) {
  const m = ENGINES[entry.kind]
  const [removing, setRemoving] = useState(false)
  const [removeError, setRemoveError] = useState<string | null>(null)

  const handleRemove = async (e: React.MouseEvent) => {
    e.stopPropagation()
    setRemoving(true)
    setRemoveError(null)
    // 6 s deadline so the × button doesn't sit stuck when the backend is
    // busy. Mirrors the enqueue/log timeout pattern.
    const deadline = new Promise<'timeout'>((resolve) =>
      window.setTimeout(() => resolve('timeout'), 6000),
    )
    try {
      const res = await Promise.race([
        apiSend('/api/jobs/queue/remove', 'POST', { kind: entry.kind }).then(
          () => 'ok' as const,
        ),
        deadline,
      ])
      if (res === 'timeout') {
        setRemoveError('Remove request slow — backend is busy.')
        window.setTimeout(() => setRemoveError(null), 4000)
      }
    } catch (err) {
      setRemoveError(err instanceof Error ? err.message : String(err))
      window.setTimeout(() => setRemoveError(null), 4000)
    } finally {
      setRemoving(false)
    }
  }

  return (
    <div
      style={{
        display: 'flex',
        alignItems: 'stretch',
        background: 'var(--well-dim)',
        border: '1px solid var(--gilt-line)',
      }}
    >
      <button
        type="button"
        onClick={() => onOpenLog(entry.kind)}
        title={`${m.label} · waiting (${QUEUE_SOURCE_LABEL[entry.source]})`}
        style={{
          display: 'flex',
          alignItems: 'center',
          gap: 7,
          padding: '4px 7px',
          background: 'transparent',
          border: 'none',
          fontFamily: 'var(--font-mono)',
          fontSize: 11,
          cursor: 'pointer',
          textAlign: 'left',
          color: 'inherit',
          flex: 1,
        }}
      >
        <span
          style={{
            width: 5,
            height: 5,
            background: m.color,
            borderRadius: '50%',
            opacity: 0.6,
          }}
        />
        <span style={{ color: 'var(--parchemin)', flex: 1 }}>{m.label}</span>
        <span
          style={{
            color: 'var(--ink-dimmer)',
            fontFamily: 'var(--font-display)',
            fontSize: 8,
            letterSpacing: '0.18em',
            textTransform: 'uppercase',
          }}
        >
          {QUEUE_SOURCE_LABEL[entry.source]}
        </span>
        <span style={{ color: 'var(--ink-dimmer)', fontSize: 10 }}>#{position}</span>
      </button>
      <button
        type="button"
        onClick={handleRemove}
        disabled={removing}
        title={
          removeError
            ? removeError
            : `Remove ${m.label} from the queue`
        }
        aria-label={`Remove ${m.label} from the queue`}
        style={{
          padding: '0 8px',
          background: removeError ? 'rgba(125,30,45,0.12)' : 'transparent',
          border: 'none',
          borderLeft: '1px solid var(--gilt-line)',
          color: removeError ? 'var(--rouge-clair)' : 'var(--ink-dim)',
          fontFamily: 'var(--font-mono)',
          fontSize: 13,
          lineHeight: 1,
          cursor: removing ? 'progress' : 'pointer',
        }}
      >
        {removing ? '…' : removeError ? '!' : '×'}
      </button>
    </div>
  )
}
