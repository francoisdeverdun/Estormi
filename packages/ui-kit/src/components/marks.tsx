/**
 * Small SVG marks reused across the design — the smallest atoms of the
 * Estormi visual language.
 *
 * Each mark is pure SVG, no DOM events, no internal state — safe to render
 * hundreds at a time.
 */

export interface FleuronProps {
  size?: number
  color?: string
  petals?: number
  hasCenter?: boolean
  opacity?: number
}

export function Fleuron({
  size = 10,
  color = 'var(--or-ancien)',
  petals = 4,
  hasCenter = true,
  opacity = 1,
}: FleuronProps) {
  const r = size / 2
  const petalLen = r * 0.85
  const petalW = r * 0.32
  return (
    <svg
      width={size}
      height={size}
      viewBox={`0 0 ${size} ${size}`}
      style={{ opacity, display: 'block' }}
      aria-hidden="true"
    >
      {Array.from({ length: petals }).map((_, i) => {
        const angle = (i * 360) / petals
        return (
          <ellipse
            key={i}
            cx={r}
            cy={r - petalLen / 2}
            rx={petalW}
            ry={petalLen / 2}
            fill={color}
            opacity="0.75"
            transform={`rotate(${angle} ${r} ${r})`}
          />
        )
      })}
      {hasCenter && <circle cx={r} cy={r} r={Math.max(1, r * 0.18)} fill={color} />}
    </svg>
  )
}

export interface DiamondProps {
  size?: number
  color?: string
  filled?: boolean
}

export function Diamond({
  size = 8,
  color = 'var(--or-ancien)',
  filled = true,
}: DiamondProps) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 10 10"
      style={{ display: 'block' }}
      aria-hidden="true"
    >
      <path
        d="M5 0 L10 5 L5 10 L0 5 Z"
        fill={filled ? color : 'none'}
        stroke={color}
        strokeWidth="0.5"
      />
    </svg>
  )
}
