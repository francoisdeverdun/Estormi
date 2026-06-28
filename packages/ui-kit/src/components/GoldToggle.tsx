/**
 * GoldToggle — gilt-rail toggle switch in the Ars Memoriae idiom.
 *
 * Visual rules:
 *   - track is a thin rounded rectangle bordered with gilt-line; ON gets
 *     a pourpre fill, OFF stays a dark recessed well.
 *   - knob is a brass disc with a tiny inner highlight; slides via CSS
 *     transition (no Framer).
 *
 * Accessibility: button[role=switch], aria-checked reflects state, focus
 * ring styled via :focus-visible. Disabled prop lowers opacity and blocks
 * the click.
 */
export interface GoldToggleProps {
  checked: boolean
  onChange: (next: boolean) => void
  ariaLabel?: string
  disabled?: boolean
  size?: 'sm' | 'md'
}

export function GoldToggle({
  checked,
  onChange,
  ariaLabel,
  disabled = false,
  size = 'md',
}: GoldToggleProps) {
  // Slimmed down for the quarter-screen one-pager — `md` is now 28×14 and
  // `sm` shrinks to 24×12. Old defaults (38×22 / 32×18) ate too much row
  // height in the dense source / parameter rows.
  const w = size === 'sm' ? 24 : 28
  const h = size === 'sm' ? 12 : 14
  const knob = h - 4
  const offset = checked ? w - knob - 2 : 2

  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      aria-label={ariaLabel}
      disabled={disabled}
      onClick={(e) => {
        e.stopPropagation()
        if (!disabled) onChange(!checked)
      }}
      style={{
        position: 'relative',
        width: w,
        height: h,
        padding: 0,
        background: checked
          ? 'linear-gradient(180deg, rgba(184,46,46,0.55), rgba(125,30,45,0.55))'
          : 'rgba(0,0,0,0.4)',
        border: `1px solid ${checked ? 'var(--pourpre-clair)' : 'var(--gilt-line)'}`,
        borderRadius: h,
        cursor: disabled ? 'not-allowed' : 'pointer',
        opacity: disabled ? 0.5 : 1,
        transition: 'background 180ms ease, border-color 180ms ease',
      }}
    >
      <span
        aria-hidden
        style={{
          position: 'absolute',
          top: 1,
          left: offset,
          width: knob,
          height: knob,
          borderRadius: '50%',
          background: checked
            ? 'linear-gradient(180deg, var(--brass-bright), var(--brass-mid))'
            : 'linear-gradient(180deg, var(--brass-deep), var(--brass-dark))',
          boxShadow: '0 1px 2px var(--shadow-faint)',
          transition: 'left 180ms ease, background 180ms ease',
        }}
      />
    </button>
  )
}
