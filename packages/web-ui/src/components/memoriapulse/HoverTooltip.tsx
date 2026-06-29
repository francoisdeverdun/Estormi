/**
 * HoverTooltip — the per-day source breakdown surfaced when the user hovers a
 * day on the MemoriaPulse stacked-area chart. Sorts visible sources by
 * contribution (desc), keeps hidden sources below in struck-through grey, and
 * renders the date as a dotted day·month·year folio. Extracted from MemoriaPulse.tsx.
 */
import { fmtInt } from '../../lib/format'
import { colourFor, type TimeseriesResponse } from './shared'

interface HoverTooltipProps {
  day: TimeseriesResponse['series'][number]
  sources: string[]
  visible: string[]
}

export function HoverTooltip({ day, sources, visible }: HoverTooltipProps) {
  const visSet = new Set(visible)
  const visibleTotal = visible.reduce((acc, s) => acc + (day.by_source?.[s] ?? 0), 0)
  // Sort visible sources by contribution (desc); hidden sources go below in grey
  // so the user knows what they've toggled off without losing the column.
  const rows = sources
    .map((s, i) => ({
      name: s,
      colour: colourFor(i),
      n: day.by_source?.[s] ?? 0,
      visible: visSet.has(s),
    }))
    .sort((a, b) => {
      if (a.visible !== b.visible) return a.visible ? -1 : 1
      return b.n - a.n
    })
    .filter((r) => r.n > 0)

  return (
    <div
      role="tooltip"
      style={{
        position: 'absolute',
        top: 4,
        right: 4,
        minWidth: 160,
        padding: '8px 10px',
        background: 'rgba(20,20,28,0.92)',
        border: '1px solid var(--gilt-line-strong)',
        fontFamily: 'var(--font-mono)',
        fontSize: 12,
        color: 'var(--parchemin)',
        pointerEvents: 'none',
        zIndex: 2,
      }}
    >
      <div
        style={{
          fontFamily: 'var(--font-display)',
          fontSize: 11,
          letterSpacing: '0.22em',
          color: 'var(--or-ancien)',
          textTransform: 'uppercase',
          marginBottom: 4,
          display: 'flex',
          justifyContent: 'space-between',
          gap: 8,
        }}
      >
        <span title={day.day}>{folioDate(day.day)}</span>
        <span>{fmtInt(visibleTotal)}</span>
      </div>
      {rows.length === 0 ? (
        <div style={{ color: 'var(--ink-dim)' }}>no activity</div>
      ) : (
        rows.map((r) => (
          <div
            key={r.name}
            style={{
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'space-between',
              gap: 10,
              padding: '1px 0',
              color: r.visible ? 'var(--parchemin)' : 'var(--ink-dimmer)',
              textDecoration: r.visible ? 'none' : 'line-through',
            }}
          >
            <span style={{ display: 'inline-flex', alignItems: 'center', gap: 6 }}>
              <span
                aria-hidden="true"
                style={{
                  display: 'inline-block',
                  width: 6,
                  height: 6,
                  background: r.colour,
                  transform: 'rotate(45deg)',
                  opacity: r.visible ? 1 : 0.4,
                }}
              />
              {r.name}
            </span>
            <span>{fmtInt(r.n)}</span>
          </div>
        ))
      )}
    </div>
  )
}

/** Convert an ISO ``YYYY-MM-DD`` to a dotted day·month·year folio date — e.g.
 *  ``2026-05-15`` → ``15 · 05 · 2026``. Falls back to the input on parse
 *  failure so the tooltip never goes blank. */
function folioDate(iso: string): string {
  const m = iso.match(/^(\d{4})-(\d{2})-(\d{2})$/)
  if (!m) return iso
  const [, y, mo, d] = m
  return `${d} · ${mo} · ${y}`
}
