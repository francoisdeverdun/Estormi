/**
 * SourceHistoryModal — continuous live-tail log for a single ingestion source.
 *
 * The earlier version split the log into per-run rows; the user found that
 * fragmentation more confusing than helpful and asked for a single
 * continuous-timeline format. This rewrite drops
 * the run picker entirely and shows the source's full log file — every
 * timestamped line, with a blank line between runs as they appear in the
 * log itself.
 *
 * Three stacked panes:
 *   1. Header — source name + live status + duration
 *   2. Log    — auto-refreshing tail of the stage's log file (live)
 *
 * The scrim + dialog mechanics (drag-guard scrim close, Escape, portal) come
 * from the shared ``ModalOverlay``.
 */
import { SOURCE_MARKER } from '../lib/logFormat'
import { ModalOverlay } from './ModalOverlay'
import { FormattedLog } from './log/LogStream'
import type { SourceRunStatus } from './SourceRow'

export interface SourceHistoryModalProps {
  /** Canonical source key, e.g. "notes" / "mail". */
  stage: string
  /** Pretty label for the header, e.g. "Apple Notes". */
  label: string
  /** Current run status of THIS source. */
  liveStatus: SourceRunStatus
  /** Live duration string ("00:42" or "3m 12s"). */
  liveDuration?: string
  onClose: () => void
}

/** Refresh cadence while the modal is open. 4 s feels live without
 *  hammering the sidecar; the source log is just a file tail so the cost
 *  is small even on a long history. */
const REFRESH_MS = 4_000

export function SourceHistoryModal({
  stage,
  label,
  liveStatus,
  liveDuration,
  onClose,
}: SourceHistoryModalProps) {
  const isRunning = liveStatus === 'running'
  const headerColor = isRunning
    ? 'var(--or-vif)'
    : liveStatus === 'fail'
      ? 'var(--rouge-clair)'
      : liveStatus === 'ok'
        ? 'var(--vert-sauge)'
        : 'var(--or-clair)'

  return (
    <ModalOverlay
      onClose={onClose}
      closeOnScrim="drag-guard"
      escape="plain"
      portal
      zIndex={220}
      scrimBackground="var(--scrim-backdrop)"
      ariaLabel={`${label} log`}
      dialogStyle={{
        width: '100%',
        maxWidth: 720,
        maxHeight: 'calc(100vh - 48px)',
        display: 'flex',
        flexDirection: 'column',
        background: 'var(--charbon)',
        border: '1px solid var(--gilt-line-strong)',
        borderTop: `3px solid ${headerColor}`,
      }}
    >
      {/* Header */}
      <div
        style={{
          padding: '10px 14px',
          borderBottom: '1px solid var(--gilt-line)',
          display: 'flex',
          alignItems: 'center',
          gap: 10,
        }}
      >
        <span
          aria-hidden="true"
          style={{
            width: 8,
            height: 8,
            borderRadius: '50%',
            background: headerColor,
            boxShadow: isRunning ? `0 0 6px ${headerColor}` : 'none',
            flexShrink: 0,
          }}
        />
        <div style={{ flex: 1, minWidth: 0 }}>
          <div
            style={{
              fontFamily: 'var(--font-display)',
              fontSize: 13,
              letterSpacing: '0.22em',
              textTransform: 'uppercase',
              color: headerColor,
              fontWeight: 700,
              whiteSpace: 'nowrap',
              overflow: 'hidden',
              textOverflow: 'ellipsis',
            }}
          >
            {label} · log
          </div>
          <div
            style={{
              fontFamily: 'var(--font-mono)',
              fontSize: 11,
              color: 'var(--ink-dim)',
              marginTop: 2,
            }}
          >
            {isRunning
              ? `running${liveDuration ? ` · ${liveDuration}` : ''} · live tail`
              : liveStatus === 'idle'
                ? 'idle · live tail'
                : `${liveStatus}${liveDuration ? ` · ${liveDuration}` : ''} · live tail`}
          </div>
        </div>
        <button
          type="button"
          onClick={onClose}
          aria-label="Close"
          style={{
            padding: '3px 9px',
            background: 'transparent',
            border: '1px solid var(--gilt-line-strong)',
            color: 'var(--ink-dim)',
            fontFamily: 'var(--font-display)',
            fontSize: 10,
            letterSpacing: '0.18em',
            textTransform: 'uppercase',
            cursor: 'pointer',
          }}
        >
          Close
        </button>
      </div>

      {/* Continuous, formatted log pane — the full per-source history
          (every run), with run boundaries drawn as separators and stage
          transitions highlighted. Same formatted renderer as the briefing
          Atelier; chronological with auto-scroll while the source runs. */}
      <div
        style={{
          flex: 1,
          minHeight: 160,
          display: 'flex',
          flexDirection: 'column',
          overflow: 'hidden',
          background: 'var(--encre)',
        }}
      >
        <FormattedLog
          url={`/api/pipeline/stage-log?source=${encodeURIComponent(stage)}&lines=2000`}
          pollMs={REFRESH_MS}
          parseOpts={{ markerRe: SOURCE_MARKER }}
          autoScroll={isRunning}
        />
      </div>
    </ModalOverlay>
  )
}
