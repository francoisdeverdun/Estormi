/**
 * ProcessionNode — one station in the horizontal ingestion procession: an
 * app-icon disc clipped to a gilt-ringed circle, sitting on a rail that fills
 * left→right with gold up to the running stage. A label + chunk yield / live
 * timer sit beneath. Selecting it opens that stage's log below. Extracted from
 * StageProcession.tsx.
 */
import { BrandIcon } from '../../BrandIcon'
import type { PipelineStage } from '../../../api/pipeline'
import { STATION, fmtClock, stageLabel, stageVisual } from './shared'

export function ProcessionNode({
  stage,
  isFirst,
  leftFilled,
  rightFilled,
  chunks,
  selected,
  now,
  onSelect,
}: {
  stage: PipelineStage
  isFirst: boolean
  leftFilled: boolean
  rightFilled: boolean
  chunks: number
  selected: boolean
  now: number
  onSelect: () => void
}) {
  const v = stageVisual(stage.status)
  const dim = stage.status === 'pending'
  let foot = stage.status
  if (v.live && stage.started_at_epoch_ms) {
    foot = fmtClock(Math.max(0, Math.floor((now - stage.started_at_epoch_ms) / 1000)))
  } else if (chunks > 0) {
    foot = `+${chunks.toLocaleString()}`
  } else if (stage.duration && stage.duration !== '—') {
    foot = stage.duration
  }
  const footColor = v.live ? v.color : chunks > 0 ? 'var(--or-clair)' : 'var(--ink-dimmer)'
  const fill = (on: boolean) => (on ? 'var(--or-ancien)' : 'var(--gilt-line)')

  return (
    <button
      type="button"
      onClick={onSelect}
      title={`${stageLabel(stage.name)} · ${stage.status}`}
      style={{
        display: 'flex',
        flexDirection: 'column',
        alignItems: 'center',
        flex: '1 1 0',
        minWidth: 0,
        gap: 3,
        padding: '0 1px 1px',
        background: selected ? 'var(--encre)' : 'transparent',
        border: 'none',
        cursor: 'pointer',
        color: 'inherit',
      }}
    >
      {/* Rail + station disc */}
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
            overflow: 'hidden',
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
            opacity: dim ? 0.5 : 1,
            animation: v.live ? 'estormi-pulse 1.6s ease-in-out infinite' : undefined,
          }}
        >
          <BrandIcon source={stage.name} size={STATION.disc - 4} />
        </span>
        {/* Terminal-status corner badge */}
        {(stage.status === 'ok' || stage.status === 'fail' || stage.status === 'cancelled') && (
          <span
            aria-hidden
            style={{
              position: 'absolute',
              left: `calc(50% + ${STATION.disc / 2 - 8}px)`,
              top: `calc(50% + ${STATION.disc / 2 - 10}px)`,
              width: 12,
              height: 12,
              borderRadius: '50%',
              background: 'var(--charbon)',
              border: `1px solid ${v.color}`,
              color: v.color,
              fontFamily: 'var(--font-mono)',
              fontSize: 8,
              lineHeight: '10px',
              textAlign: 'center',
            }}
          >
            {v.glyph}
          </span>
        )}
      </span>

      {/* Label — wraps to 2 lines so narrow columns stay readable without scroll */}
      <span
        style={{
          fontFamily: 'var(--font-display)',
          fontSize: 8,
          letterSpacing: '0.02em',
          lineHeight: 1.1,
          textAlign: 'center',
          color: selected ? 'var(--or-clair)' : dim ? 'var(--ink-dim)' : 'var(--parchemin)',
          maxWidth: '100%',
          display: '-webkit-box',
          WebkitLineClamp: 2,
          WebkitBoxOrient: 'vertical',
          overflow: 'hidden',
        }}
      >
        {stageLabel(stage.name)}
      </span>
      {/* Footer — live timer / chunk yield / duration / status */}
      <span
        style={{
          fontFamily: 'var(--font-mono)',
          fontSize: 8,
          lineHeight: 1,
          minHeight: 9,
          color: footColor,
        }}
      >
        {foot}
      </span>
    </button>
  )
}
