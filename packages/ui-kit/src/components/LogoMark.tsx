/**
 * EstormiLogoMark — the blocked illuminated initial.
 *
 * A coloured ground (burgundy by default), one heavy gold keyline, and the
 * gold Cinzel majuscule: a bold, modern take on a manuscript lettrine that
 * holds up at any size. This is the brand mark — the same artwork as the app
 * icon and the masthead. The thin bracket-frame `IlluminatedCap` is the
 * in-content drop cap; this solid device is the logo.
 *
 * Mirrors EstormiLogoMark in apps/estormi-ios/Sources/Design/Brand.swift so the
 * two surfaces share one identity.
 *
 * @example
 *   <EstormiLogoMark letter="E" size={44} />
 */

export interface EstormiLogoMarkProps {
  letter?: string
  size?: number
  /** Logo ground. Defaults to the house burgundy; pass any CSS colour. */
  ground?: string
  /** Gradient id seed; only matters when several marks share one page. */
  seed?: number
}

export function EstormiLogoMark({
  letter = 'E',
  size = 44,
  ground = 'var(--brand-burgundy)',
  seed = 1,
}: EstormiLogoMarkProps) {
  // viewBox is 0..100; ratios match the iOS mark exactly.
  const border = 5.5 // size * 0.055
  const inset = 5 // size * 0.05
  const gradientId = `logo-gold-${seed}`

  return (
    <span
      style={{
        display: 'inline-block',
        width: size,
        height: size,
        minWidth: size,
        flexShrink: 0,
        lineHeight: 0,
      }}
      aria-label={letter}
      role="img"
    >
      <svg width={size} height={size} viewBox="0 0 100 100" style={{ display: 'block' }}>
        <defs>
          {/* Vertical gilt sheen, light → deep gold (matches the iOS
              capGradient and the IlluminatedCap inline gradient — note this
              is a distinct 4-stop ramp from the --gilt-gradient token). */}
          <linearGradient id={gradientId} x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor="#FFE9B8" />
            <stop offset="40%" stopColor="#DCBA8A" />
            <stop offset="75%" stopColor="#C8A96B" />
            <stop offset="100%" stopColor="#8A6A30" />
          </linearGradient>
        </defs>

        {/* Burgundy ground. */}
        <rect x={0} y={0} width={100} height={100} rx={20} ry={20} fill={ground} />

        {/* Gold keyline, inset and concentric to the ground. */}
        <rect
          x={inset + border / 2}
          y={inset + border / 2}
          width={100 - 2 * (inset + border / 2)}
          height={100 - 2 * (inset + border / 2)}
          rx={15}
          ry={15}
          fill="none"
          stroke={`url(#${gradientId})`}
          strokeWidth={border}
        />

        {/* Gold majuscule. */}
        <text
          x={50}
          y={50.5}
          textAnchor="middle"
          dominantBaseline="central"
          fontFamily="'Cinzel', serif"
          fontSize={60}
          fontWeight={700}
          fill={`url(#${gradientId})`}
          stroke="rgba(0,0,0,0.3)"
          strokeWidth={0.4}
          style={{ paintOrder: 'stroke fill' }}
        >
          {letter}
        </text>
      </svg>
    </span>
  )
}
