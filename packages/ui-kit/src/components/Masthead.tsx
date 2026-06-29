/**
 * EstormiMasthead + IlluminatedRule — the brand lockup.
 *
 * Mirrors EstormiMasthead / IlluminatedRule in
 * apps/estormi-ios/Sources/Design/Brand.swift so the macOS launch cover and the
 * briefing share one masthead with the iOS Briefings page:
 *
 *   [E mark]  STORMI
 *      ❦  ARS MEMORIAE  ❦
 *   ──────◈◆◈──────
 *
 * The rule is the "modern-minimal" manuscript divider — geometry and air (a
 * fading hairline, quatrefoils, a burgundy centre lozenge) instead of a crowded
 * floral vine.
 */
import { Fleuron } from './marks'
import { EstormiLogoMark } from './LogoMark'

/* ───────────────────────── Illuminated rule ───────────────────────── */

export interface IlluminatedRuleProps {
  /** Total height of the rule band (the ornament scales within it). */
  height?: number
  width?: number | string
}

/**
 * A minimalist gilt divider: a hairline that fades at both ends, a centred
 * burgundy lozenge in a gold frame, flanking quatrefoils, and outboard
 * lozenges. The fading hairlines flex to fill; the ornament cluster is a
 * fixed-aspect SVG so its circles stay round at any width.
 */
export function IlluminatedRule({ height = 18, width = '100%' }: IlluminatedRuleProps) {
  const gold = 'var(--or-ancien)'
  const goldBright = 'var(--or-clair)'
  const accent = 'var(--brand-burgundy)'
  const cy = 9
  const cx = 70

  const diamond = (x: number, y: number, r: number) =>
    `M ${x} ${y - r} L ${x + r} ${y} L ${x} ${y + r} L ${x - r} ${y} Z`

  // Quatrefoil: four gold petals around a burgundy heart.
  const quatrefoil = (qx: number) => {
    const petal = 1.7
    const off = petal * 1.5
    const petals: Array<[number, number]> = [
      [0, -off],
      [off, 0],
      [0, off],
      [-off, 0],
    ]
    return (
      <g key={`q${qx}`}>
        {petals.map(([dx, dy], i) => (
          <circle key={i} cx={qx + dx} cy={cy + dy} r={petal} fill={gold} />
        ))}
        <circle cx={qx} cy={cy} r={petal * 0.85} fill={accent} />
      </g>
    )
  }

  const inner = 18
  const outer = 48

  return (
    <div
      style={{ display: 'flex', alignItems: 'center', gap: 0, width, height }}
      aria-hidden="true"
    >
      <span
        style={{
          flex: 1,
          height: 1,
          background: `linear-gradient(to right, transparent, ${gold} 70%)`,
          opacity: 0.85,
        }}
      />
      <svg
        width={140}
        height={height}
        viewBox={`0 0 140 18`}
        style={{ display: 'block', flexShrink: 0 }}
      >
        {/* Outboard lozenges with a faint pip beyond. */}
        {[cx - outer, cx + outer].map((sx) => (
          <g key={`o${sx}`}>
            <path d={diamond(sx, cy, 2)} fill={gold} />
            <circle cx={sx + (sx < cx ? -6 : 6)} cy={cy} r={0.9} fill={gold} opacity={0.7} />
          </g>
        ))}
        {/* Quatrefoils flanking the centre. */}
        {[cx - inner, cx + inner].map((qx) => quatrefoil(qx))}
        {/* Centre: burgundy lozenge in a thin gold frame, bright gold heart. */}
        <path d={diamond(cx, cy, 5.5)} fill={accent} stroke={gold} strokeWidth={0.9} />
        <circle cx={cx} cy={cy} r={1.1} fill={goldBright} />
      </svg>
      <span
        style={{
          flex: 1,
          height: 1,
          background: `linear-gradient(to left, transparent, ${gold} 70%)`,
          opacity: 0.85,
        }}
      />
    </div>
  )
}

/* ───────────────────────────── Masthead ───────────────────────────── */

export interface EstormiMastheadProps {
  /** Logo-mark size; everything else scales from it (iOS default 54). */
  markSize?: number
  /** Constrain the rule width under the wordmark (default follows content). */
  ruleWidth?: number | string
}

export function EstormiMasthead({ markSize = 54, ruleWidth }: EstormiMastheadProps) {
  return (
    <div
      style={{
        display: 'flex',
        flexDirection: 'column',
        alignItems: 'center',
        gap: 8,
        width: '100%',
      }}
    >
      {/* Mark + wordmark — a gilded device set off from the word, a lockup
          rather than a single run-together word. */}
      <div style={{ display: 'flex', alignItems: 'center', gap: markSize * 0.16 }}>
        <EstormiLogoMark letter="E" size={markSize} />
        <span
          style={{
            fontFamily: 'var(--font-display)',
            fontSize: Math.round(markSize * 0.62),
            fontWeight: 700,
            letterSpacing: '0.06em',
            color: 'var(--parchemin-os)',
            lineHeight: 1,
          }}
        >
          STORMI
        </span>
      </div>
      {/* Garlanded tagline. */}
      <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
        <Fleuron size={10} color="var(--or-sombre)" />
        <span
          style={{
            fontFamily: 'var(--font-display)',
            fontSize: 11,
            fontWeight: 700,
            letterSpacing: '0.42em',
            // pull the trailing letter-spacing back so the word stays centred
            // between the two fleurons.
            textIndent: '0.42em',
            color: 'var(--or-clair)',
            textTransform: 'uppercase',
          }}
        >
          Ars Memoriae
        </span>
        <Fleuron size={10} color="var(--or-sombre)" />
      </div>
      <IlluminatedRule width={ruleWidth ?? '100%'} />
    </div>
  )
}
