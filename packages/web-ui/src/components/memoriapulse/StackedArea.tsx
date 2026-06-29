/**
 * StackedArea — the pure-SVG stacked-area renderer behind MemoriaPulse.
 *
 * Each visible source is a band with a diagonal-hatch fill and a jittered,
 * scribed-by-hand top contour; gilt folio rulings sit behind, and a brass
 * marker rod replaces the crosshair on hover. Extracted from MemoriaPulse.tsx.
 */
import { useRef } from 'react'
import { colourFor, type TimeseriesResponse } from './shared'

interface StackedAreaProps {
  series: TimeseriesResponse['series']
  /** All sources in the dataset; used to look up the colour index so the
   *  same source keeps the same colour even when others are toggled off. */
  sources: string[]
  visibleSources: string[]
  hoverDayIdx: number | null
  onHoverDay: (idx: number | null) => void
  ariaLabel: string
}

export function StackedArea({
  series,
  sources,
  visibleSources,
  hoverDayIdx,
  onHoverDay,
  ariaLabel,
}: StackedAreaProps) {
  const W = 600
  const H = 160
  const padBottom = 4
  const usableH = H - padBottom
  const svgRef = useRef<SVGSVGElement | null>(null)

  if (series.length === 0 || visibleSources.length === 0) {
    return (
      <svg
        viewBox={`0 0 ${W} ${H}`}
        width="100%"
        height="100%"
        preserveAspectRatio="none"
        role="img"
        aria-label={ariaLabel}
        style={{ display: 'block' }}
      >
        {gildedRulings(W, H)}
      </svg>
    )
  }

  // Per-day cumulative stack across visible sources only.
  const cumulative: number[][] = series.map(() => Array(visibleSources.length).fill(0))
  for (let di = 0; di < series.length; di++) {
    let acc = 0
    for (let si = 0; si < visibleSources.length; si++) {
      acc += series[di].by_source?.[visibleSources[si]] ?? 0
      cumulative[di][si] = acc
    }
  }
  const maxY = Math.max(1, ...cumulative.map((row) => row[row.length - 1]))
  const xFor = (di: number) =>
    series.length === 1 ? W / 2 : (di / (series.length - 1)) * W
  // Deterministic sub-pixel jitter on every stacked top y so the curve reads
  // hand-scribed. Seeded by (di, si) so the same data renders identically
  // across re-renders — no animation, just texture.
  const jitter = (di: number, si: number) => {
    const h = ((di * 131 + si * 17 + 7) % 100) / 100 - 0.5
    return h * 1.6
  }
  const yFor = (v: number) => usableH - (v / maxY) * usableH + 1
  const yForTop = (di: number, si: number) => yFor(cumulative[di][si]) + jitter(di, si)

  // Each band's filled path — top edge jittered, bottom edge follows the
  // *previous* band's jittered top so adjacent bands share a seam (no gap).
  const bandPath = (si: number): string => {
    const top = series.map((_, di) => `${xFor(di)},${yForTop(di, si)}`)
    const bottom = series.map((_, di) =>
      si === 0
        ? `${xFor(di)},${yFor(0)}`
        : `${xFor(di)},${yForTop(di, si - 1)}`,
    )
    return `M ${top.join(' L ')} L ${bottom.reverse().join(' L ')} Z`
  }

  // The same top edge as a stand-alone polyline so we can stroke it darker
  // than the hatched fill — that gives the scribed contour its weight.
  const bandTopPath = (si: number): string => {
    const top = series.map((_, di) => `${xFor(di)},${yForTop(di, si)}`)
    return `M ${top.join(' L ')}`
  }

  const handlePointerMove = (e: React.PointerEvent<SVGSVGElement>) => {
    const svg = svgRef.current
    if (!svg) return
    const rect = svg.getBoundingClientRect()
    const rel = (e.clientX - rect.left) / rect.width // 0..1
    const idx = Math.min(series.length - 1, Math.max(0, Math.round(rel * (series.length - 1))))
    onHoverDay(idx)
  }

  return (
    <svg
      ref={svgRef}
      viewBox={`0 0 ${W} ${H}`}
      width="100%"
      height="100%"
      preserveAspectRatio="none"
      role="img"
      aria-label={ariaLabel}
      style={{ display: 'block', cursor: 'crosshair' }}
      onPointerMove={handlePointerMove}
      onPointerLeave={() => onHoverDay(null)}
    >
      <defs>
        {/* One diagonal-hatch pattern per visible source. Hatch lines are
            drawn in the source colour at ~55% opacity; the surrounding
            <path> then strokes the top contour at full opacity for the
            scribed-ink read. patternUnits="userSpaceOnUse" so the hatch
            angle stays consistent regardless of viewBox scaling. */}
        {visibleSources.map((s) => {
          const colour = colourFor(sources.indexOf(s))
          const id = hatchId(s)
          return (
            <pattern
              key={id}
              id={id}
              patternUnits="userSpaceOnUse"
              width={6}
              height={6}
              patternTransform="rotate(45)"
            >
              <line
                x1="0"
                y1="0"
                x2="0"
                y2="6"
                stroke={colour}
                strokeWidth={1.4}
                strokeOpacity={0.62}
              />
            </pattern>
          )
        })}
      </defs>
      {gildedRulings(W, H)}
      {/* Stacked bands: hatched fill + jittered scribed contour on top.
          Order: fills first, then contours, so every contour reads above
          adjacent fills regardless of stack order. */}
      {visibleSources.map((s, si) => (
        <path
          key={`fill-${s}`}
          d={bandPath(si)}
          fill={`url(#${hatchId(s)})`}
          stroke="none"
        />
      ))}
      {visibleSources.map((s, si) => (
        <path
          key={`stroke-${s}`}
          d={bandTopPath(si)}
          fill="none"
          stroke={colourFor(sources.indexOf(s))}
          strokeWidth={0.9}
          strokeOpacity={0.95}
          strokeLinecap="round"
          strokeLinejoin="round"
        />
      ))}
      {hoverDayIdx !== null && (
        <BrassMarker x={xFor(hoverDayIdx)} H={H} />
      )}
    </svg>
  )
}

/** Stable hatch-pattern id derived from the source name so the legend chip
 *  and the band reference the same definition. Sanitised to a CSS-safe form
 *  (sources may contain dots or slashes). */
function hatchId(source: string): string {
  return `pulse-hatch-${source.replace(/[^a-z0-9_-]/gi, '-')}`
}

/** Hairline horizontal rulings at quarters — the gilt lines real scribes
 *  drew before writing on a folio. One solid stroke, very low alpha. */
function gildedRulings(W: number, H: number) {
  return [H * 0.25, H * 0.5, H * 0.75].map((y) => (
    <line
      key={y}
      x1="0"
      y1={y}
      x2={W}
      y2={y}
      stroke="var(--or-ancien)"
      strokeWidth="0.4"
      opacity="0.22"
    />
  ))
}

/** Brass marker rod that replaces the dashed crosshair on hover —
 *  a thin gilt vertical with a diamond cap at the top. */
function BrassMarker({ x, H }: { x: number; H: number }) {
  return (
    <g pointerEvents="none">
      <line
        x1={x}
        x2={x}
        y1={6}
        y2={H}
        stroke="var(--or-clair)"
        strokeWidth={0.9}
        strokeOpacity={0.85}
      />
      <rect
        x={x - 3}
        y={3}
        width={6}
        height={6}
        transform={`rotate(45 ${x} 6)`}
        fill="var(--or-clair)"
        stroke="var(--or-ancien)"
        strokeWidth={0.6}
      />
    </g>
  )
}
