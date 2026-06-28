/**
 * MemoriaPulse — 14-day stacked-area chart, one band per source.
 *
 * Self-fetches `/api/timeseries?days=14&mode=memory` (the `mode=memory`
 * param selects the cumulative-store series); the matrix is already in the
 * response under `series[].by_source`. Plots the cumulative *store* per
 * source so the stack climbs monotonically — the same "total over time"
 * idiom as the iOS companion's Memoria card. Each source has a colour band;
 * the legend doubles as a per-source toggle (click to hide/show). Hovering a
 * day surfaces a breakdown of every visible source's value, sorted by
 * contribution.
 *
 * Visual idiom — *illuminated manuscript*. Each band is rendered as a
 * stroked contour with a diagonal-hatch fill (see StackedArea), and the
 * legend uses lozenge markers ❖ over the shared viz palette. The SVG renderer
 * (StackedArea) and the hover breakdown (HoverTooltip) live in `memoriapulse/`.
 *
 * Pure SVG, no chart library — the hero stays self-contained and we don't
 * pull in a charting dep just for this strip.
 */
import { useEffect, useMemo, useState } from 'react'
import { apiGet } from '../api/client'
import { StripHeader } from './StripHeader'
import { StackedArea } from './memoriapulse/StackedArea'
import { HoverTooltip } from './memoriapulse/HoverTooltip'
import { colourFor, type TimeseriesResponse } from './memoriapulse/shared'

export interface MemoriaPulseProps {
  /** Override the small uppercase label above the title (defaults to "Pulse"). */
  eyebrowOverride?: string
  /** Override the H2-level title (defaults to "14 days · by source"). */
  titleOverride?: string
}

export function MemoriaPulse({
  eyebrowOverride,
  titleOverride,
}: MemoriaPulseProps = {}) {
  const [data, setData] = useState<TimeseriesResponse | null>(null)
  const [error, setError] = useState(false)
  const [hiddenSources, setHiddenSources] = useState<Set<string>>(new Set())
  const [hoverDayIdx, setHoverDayIdx] = useState<number | null>(null)

  useEffect(() => {
    let alive = true
    const load = () =>
      apiGet<TimeseriesResponse>('/api/timeseries?days=14&mode=memory')
        .then((r) => {
          if (!alive) return
          setData(r)
          setError(false)
        })
        // Keep the last good `data` on a transient failure so a backend blip
        // doesn't flash an empty chart; only flag the error state. A genuinely
        // empty series (no activity yet) returns ok with empty arrays and is
        // distinct from this.
        .catch(() => alive && setError(true))
    void load()
    // Refresh on the same cadence as the rest of the dashboard so a chart left
    // open tracks new ingestion instead of freezing at its mount-time snapshot.
    const id = window.setInterval(() => void load(), 30_000)
    return () => {
      alive = false
      window.clearInterval(id)
    }
  }, [])

  const allSources = (data?.sources ?? []).filter(Boolean)
  const visibleSources = useMemo(
    () => allSources.filter((s) => !hiddenSources.has(s)),
    [allSources, hiddenSources],
  )
  const series = useMemo(() => data?.series ?? [], [data])
  const visibleDailyTotals = useMemo(
    () =>
      series.map((d) =>
        visibleSources.reduce((acc, s) => acc + (d.by_source?.[s] ?? 0), 0),
      ),
    [series, visibleSources],
  )
  const hasData = visibleDailyTotals.some((v) => v > 0)

  const toggleSource = (s: string) => {
    setHiddenSources((prev) => {
      const next = new Set(prev)
      if (next.has(s)) next.delete(s)
      else next.add(s)
      return next
    })
  }

  const hoverDay = hoverDayIdx !== null ? series[hoverDayIdx] : null

  return (
    <div style={{ display: 'flex', flexDirection: 'column', minWidth: 0 }}>
      <StripHeader
        eyebrow={eyebrowOverride ?? "Pulse"}
        title={titleOverride ?? "14 days · by source"}
      />
      <div style={{ flex: 1, position: 'relative', minHeight: 140 }}>
        <StackedArea
          series={series}
          sources={allSources}
          visibleSources={visibleSources}
          hoverDayIdx={hoverDayIdx}
          onHoverDay={setHoverDayIdx}
          ariaLabel={`14-day cumulative memory store per source${
            error && !hasData
              ? ' (unavailable)'
              : hasData
                ? ''
                : ' (no activity)'
          }`}
        />
        {hoverDay && (
          <HoverTooltip day={hoverDay} sources={allSources} visible={visibleSources} />
        )}
        {error && !hasData && (
          <div
            role="alert"
            style={{
              position: 'absolute',
              inset: 0,
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'center',
              fontFamily: 'var(--font-mono)',
              fontSize: 11,
              color: 'var(--ink-dim)',
              fontStyle: 'italic',
              pointerEvents: 'none',
            }}
          >
            « pulse unavailable »
          </div>
        )}
        {!hasData && !error && (
          <div
            style={{
              position: 'absolute',
              inset: 0,
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'center',
              fontFamily: 'var(--font-mono)',
              fontSize: 11,
              color: 'var(--ink-dim)',
              fontStyle: 'italic',
              pointerEvents: 'none',
            }}
          >
            « no activity yet — run a source to populate the graph »
          </div>
        )}
      </div>

      <div
        style={{
          display: 'flex',
          flexWrap: 'wrap',
          alignItems: 'center',
          gap: '4px 10px',
          marginTop: 10,
        }}
        aria-label="Sources — click a chip to hide or show its band"
      >
        {allSources.map((s, i) => {
          const hidden = hiddenSources.has(s)
          const colour = colourFor(i)
          return (
            <span key={s} style={{ display: 'inline-flex', alignItems: 'center', gap: 4 }}>
              <button
                type="button"
                onClick={() => toggleSource(s)}
                aria-pressed={!hidden}
                title={hidden ? `Show ${s}` : `Hide ${s}`}
                style={{
                  display: 'inline-flex',
                  alignItems: 'center',
                  gap: 5,
                  padding: '2px 4px',
                  background: 'transparent',
                  border: 'none',
                  color: hidden ? 'var(--ink-dimmer)' : 'var(--parchemin)',
                  fontFamily: 'var(--font-display)',
                  fontSize: 11,
                  letterSpacing: '0.14em',
                  textTransform: 'uppercase',
                  cursor: 'pointer',
                  opacity: hidden ? 0.5 : 1,
                  textDecoration: hidden ? 'line-through' : 'none',
                  transition: 'opacity 120ms, color 120ms',
                }}
              >
                {/* Lozenge marker — a CSS-rotated square keeps it crisp. */}
                <span
                  aria-hidden="true"
                  style={{
                    display: 'inline-block',
                    width: 7,
                    height: 7,
                    background: colour,
                    transform: 'rotate(45deg)',
                    boxShadow: hidden ? 'none' : `0 0 4px ${colour}55`,
                    opacity: hidden ? 0.35 : 1,
                  }}
                />
                {s}
              </button>
            </span>
          )
        })}
      </div>

      {/* Peak / Avg footer dropped per user — the chart itself shows the
          shape, the numeric summary was duplicate UI weight. */}
    </div>
  )
}
