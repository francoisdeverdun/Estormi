/**
 * EngineLogModal — full-screen logs view for one engine, opened from the
 * engine-room popover (current/last job row, queue rows, or engines grid).
 * Lifted out of EngineRoomPopover.tsx.
 *
 * The scrim + dialog mechanics (drag-guard scrim close, Escape, portal — it
 * must sit above the popover via zIndex 220) come from the shared
 * ``ModalOverlay``.
 */
import {
  ENGINES,
  humanAgo,
  useSystemStatus,
  type EngineKind,
} from '../../state/SystemStatus'
import { ModalOverlay } from '../ModalOverlay'
import { BriefingAtelier } from '../briefing/BriefingAtelier'
import { FormattedLog } from '../log/LogStream'
import { IngestionStageBody } from './StageProcession'
import { LiveDot } from './LiveDot'

// Distill writes one file (<data dir>/logs/distill.log); poll at the same
// cadence as the briefing atelier (endpoint is rate-limited to 30/min).
const DISTILL_LOG_URL = '/api/distill/log?lines=600'
const RUNNING_POLL_MS = 2500

export function EngineLogModal({
  kind,
  onClose,
}: {
  kind: EngineKind
  onClose: () => void
}) {
  const sys = useSystemStatus()
  const meta = ENGINES[kind]

  const isCurrent = sys.job === kind
  const isQueued = sys.queue.some((e) => e.kind === kind)
  const lastJob = sys.lastJob && sys.lastJob.kind === kind ? sys.lastJob : null

  return (
    <ModalOverlay
      onClose={onClose}
      closeOnScrim="drag-guard"
      escape="plain"
      portal
      zIndex={220}
      scrimBackground="var(--scrim-backdrop-soft)"
      ariaLabel={`${meta.label} logs`}
      dialogStyle={{
        width: 'min(720px, 100%)',
        maxHeight: 'calc(100vh - 64px)',
        display: 'flex',
        flexDirection: 'column',
        background: 'var(--charbon)',
        border: '1px solid var(--gilt-line-strong)',
        borderTop: `3px solid ${meta.color}`,
        borderRadius: 'var(--radius-panel)',
        overflow: 'hidden',
        boxShadow: '0 20px 60px var(--shadow-soft)',
      }}
    >
      <div
        style={{
          padding: '12px 16px',
          borderBottom: '1px solid var(--gilt-line)',
          display: 'flex',
          alignItems: 'center',
          gap: 10,
        }}
      >
        <LiveDot running={isCurrent} color={meta.color} size={9} />
        <div style={{ flex: 1, minWidth: 0 }}>
          <div
            style={{
              fontFamily: 'var(--font-display)',
              fontSize: 12,
              letterSpacing: '0.2em',
              textTransform: 'uppercase',
              color: meta.color,
              fontWeight: 700,
            }}
          >
            {meta.label} · logs
          </div>
          <div
            style={{
              fontFamily: 'var(--font-mono)',
              fontSize: 11,
              color: 'var(--ink-dim)',
              marginTop: 2,
            }}
          >
            {isCurrent
              ? `running · ${meta.sub}`
              : isQueued
                ? 'queued — waiting'
                : lastJob
                  ? `last · ${humanAgo(lastJob.endedAt)} (${lastJob.status})`
                  : 'idle · no recorded work'}
          </div>
        </div>
        <button
          type="button"
          onClick={onClose}
          aria-label={"Close"}
          style={{
            padding: '4px 10px',
            background: 'transparent',
            border: '1px solid var(--gilt-line-strong)',
            borderRadius: 'var(--radius-tight)',
            color: 'var(--ink-dim)',
            fontFamily: 'var(--font-display)',
            fontSize: 10,
            letterSpacing: '0.18em',
            textTransform: 'uppercase',
            cursor: 'pointer',
          }}
        >
          {"Close"}
        </button>
      </div>
      <div
        style={{
          flex: 1,
          display: 'flex',
          flexDirection: 'column',
          overflow: 'hidden',
          background: 'var(--encre)',
        }}
      >
        {kind === 'ingestion' ? (
          <IngestionStageBody color={meta.color} />
        ) : kind === 'distill' ? (
          // Distill has its own log file + phases — never the briefing atelier
          // (which parses the briefing DAG out of the knowledge log).
          <FormattedLog url={DISTILL_LOG_URL} pollMs={RUNNING_POLL_MS} autoScroll={isCurrent} />
        ) : (
          <BriefingAtelier running={isCurrent} />
        )}
      </div>
    </ModalOverlay>
  )
}
