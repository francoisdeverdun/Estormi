/**
 * BriefingAtelier — the briefing engine's two-pane "workshop" view.
 *
 * Replaces the raw single-pane tail for the briefing engine inside
 * EngineLogModal. Top: a live flow of the pipeline's agents (sources → news →
 * extractor → enrichments → correlation → vision → critic → vault), each
 * lighting up as its marker appears in the log. Bottom: the same log, de-noised
 * and formatted (shared `LogStream`). Both derive from ONE poll of
 * /api/knowledge/log — the briefing writes a single file, so unlike ingestion
 * there is no per-stage selection. Phase parsing lives in `lib/briefingPhases`.
 */
import type { PhaseState } from '../../lib/briefingPhases'
import { parseBriefingLog } from '../../lib/briefingPhases'
import { LogStream, useLogTail } from '../log/LogStream'

const LOG_URL = '/api/knowledge/log?lines=600'

// Poll cadence while a run is live. The endpoint is rate-limited to 30/minute
// (one per 2s), so we stay safely under it — 2.5s (24/min) leaves headroom and
// is plenty of granularity for a pipeline that runs over minutes.
const RUNNING_POLL_MS = 2500

// Status → disc colour + glyph, mirroring the ingestion procession's
// `stageVisual` so both engines read in one visual language.
const PHASE_VISUAL: Record<PhaseState['status'], { color: string; glyph: string; live: boolean }> = {
  idle: { color: 'var(--gilt-line-strong)', glyph: '·', live: false },
  active: { color: 'var(--or-clair)', glyph: '◆', live: true },
  done: { color: 'var(--vert-sauge)', glyph: '✓', live: false },
  failed: { color: 'var(--rouge-clair)', glyph: '✕', live: false },
}

// Shared frieze geometry — mirrors STATION in the ingestion procession.
// Columns flex-shrink to fit the modal width with no horizontal scroll.
const STATION = { disc: 26, rail: 28 } as const

/** One station in the horizontal briefing procession — same gilt-rail + status
 *  disc vocabulary as the ingestion view, so "sources → … → vault" reads as a
 *  single illuminated flow. The rail fills left→right up to the active phase. */
function PhaseNode({
  phase,
  isFirst,
  leftFilled,
  rightFilled,
}: {
  phase: PhaseState
  isFirst: boolean
  leftFilled: boolean
  rightFilled: boolean
}) {
  const v = PHASE_VISUAL[phase.status]
  const dim = phase.status === 'idle'
  const fill = (on: boolean) => (on ? 'var(--or-ancien)' : 'var(--gilt-line)')
  const foot = phase.badge || (phase.status === 'idle' ? '' : phase.status)

  return (
    <div
      role="listitem"
      title={phase.detail || phase.label}
      style={{
        display: 'flex',
        flexDirection: 'column',
        alignItems: 'center',
        flex: '1 1 0',
        minWidth: 0,
        gap: 3,
        padding: '0 1px 1px',
      }}
    >
      {/* Rail + status disc */}
      <span style={{ position: 'relative', width: '100%', height: STATION.rail, flexShrink: 0 }}>
        {!isFirst && (
          <span style={{ position: 'absolute', left: 0, width: '50%', top: '50%', height: 2, transform: 'translateY(-50%)', background: fill(leftFilled) }} />
        )}
        <span style={{ position: 'absolute', left: '50%', right: 0, top: '50%', height: 2, transform: 'translateY(-50%)', background: fill(rightFilled) }} />
        {v.live && (
          // Centre via offsets, NOT translate: estormi-live-pulse animates
          // `transform: scale(...)`, which would clobber a centring translate
          // and shove the halo down-right of the disc.
          <span
            aria-hidden
            style={{
              position: 'absolute',
              left: `calc(50% - ${STATION.disc / 2}px)`,
              top: `calc(50% - ${STATION.disc / 2}px)`,
              width: STATION.disc,
              height: STATION.disc,
              borderRadius: '50%',
              background: v.color,
              opacity: 0.45,
              animation: 'estormi-live-pulse 1.6s ease-out infinite',
            }}
          />
        )}
        <span
          style={{
            position: 'absolute',
            left: '50%',
            top: '50%',
            width: STATION.disc,
            height: STATION.disc,
            transform: 'translate(-50%, -50%)',
            borderRadius: '50%',
            border: `2px solid ${v.color}`,
            background: 'var(--charbon)',
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
            fontFamily: 'var(--font-mono)',
            fontSize: 11,
            color: v.color,
            opacity: dim ? 0.6 : 1,
            animation: v.live ? 'estormi-pulse 1.6s ease-in-out infinite' : undefined,
          }}
        >
          {v.glyph}
        </span>
      </span>

      {/* Label — wraps to 2 lines so narrow columns stay readable without scroll */}
      <span
        style={{
          fontFamily: 'var(--font-display)',
          fontSize: 8,
          letterSpacing: '0.02em',
          lineHeight: 1.1,
          textAlign: 'center',
          color: dim ? 'var(--ink-dim)' : 'var(--parchemin)',
          maxWidth: '100%',
          display: '-webkit-box',
          WebkitLineClamp: 2,
          WebkitBoxOrient: 'vertical',
          overflow: 'hidden',
        }}
      >
        {phase.label}
      </span>
      {/* Footer — badge / status */}
      <span style={{ fontFamily: 'var(--font-mono)', fontSize: 8, lineHeight: 1, minHeight: 9, color: v.live ? v.color : 'var(--ink-dimmer)' }}>
        {foot}
      </span>
    </div>
  )
}

function PhaseFlow({ phases }: { phases: PhaseState[] }) {
  // Rail fills up to the furthest active/terminal phase; everything past it
  // stays a dim hairline.
  const progressIndex = phases.reduce((acc, p, i) => (p.status !== 'idle' ? i : acc), -1)
  const doneCount = phases.filter((p) => p.status === 'done').length
  return (
    <div style={{ flex: '0 0 auto', padding: '9px 12px 8px', borderBottom: '1px solid var(--gilt-line)' }}>
      <div
        style={{
          display: 'flex',
          alignItems: 'baseline',
          justifyContent: 'space-between',
          marginBottom: 4,
          fontFamily: 'var(--font-display)',
          fontSize: 9,
          letterSpacing: '0.22em',
          textTransform: 'uppercase',
          color: 'var(--or-ancien)',
        }}
      >
        <span>The Atelier</span>
        <span style={{ fontFamily: 'var(--font-mono)', letterSpacing: 0, color: 'var(--ink-dim)' }}>
          {doneCount}/{phases.length}
        </span>
      </div>
      <div role="list" style={{ display: 'flex', alignItems: 'flex-start', overflowX: 'auto', paddingTop: 2 }}>
        {phases.map((p, i) => (
          <PhaseNode
            key={p.id}
            phase={p}
            isFirst={i === 0}
            leftFilled={i <= progressIndex}
            rightFilled={i < progressIndex}
          />
        ))}
      </div>
    </div>
  )
}

export function BriefingAtelier({ running }: { running: boolean }) {
  const { content, loading, error } = useLogTail(LOG_URL, running ? RUNNING_POLL_MS : 0)
  const trace = parseBriefingLog(content, running)

  return (
    <div style={{ flex: 1, display: 'flex', flexDirection: 'column', overflow: 'hidden' }}>
      <PhaseFlow phases={trace.phases} />
      {loading && !content ? (
        <div style={{ flex: 1, padding: '12px 16px', fontSize: 12, color: 'var(--ink-dim)' }}>Loading logs…</div>
      ) : error && !content ? (
        <div style={{ flex: 1, padding: '12px 16px', fontSize: 12, color: 'var(--rouge-clair)' }}>{error}</div>
      ) : (
        <LogStream lines={trace.lines} autoScroll={running} />
      )}
    </div>
  )
}
