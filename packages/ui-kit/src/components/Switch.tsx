/**
 * Switch — a labelled GoldToggle.
 *
 * The gilt switch with a text (or glyph) label beside it, in one clickable
 * unit. This is the canonical on/off control for a setting that has a name —
 * e.g. the briefing / distillation schedule toggles, which previously used a
 * raw `<input type="checkbox">`.
 *
 * The label drives the same `onChange` as the knob, so clicking the word
 * toggles too. `dimWhenOff` greys the label when off, reinforcing — together
 * with the dependent field's own `disabled` dimming — that the setting (and
 * whatever it controls) is inactive.
 *
 * Accessibility rides on the inner GoldToggle (`role=switch`, `aria-checked`).
 * Pass `ariaLabel` when the visible label isn't descriptive on its own.
 */
import { GoldToggle } from './GoldToggle'

export interface SwitchProps {
  checked: boolean
  onChange: (next: boolean) => void
  /** Visible label beside the knob (text or a glyph). */
  label?: React.ReactNode
  ariaLabel?: string
  disabled?: boolean
  size?: 'sm' | 'md'
  title?: string
  /** Dim the label when off (default `false`). */
  dimWhenOff?: boolean
}

export function Switch({
  checked,
  onChange,
  label,
  ariaLabel,
  disabled = false,
  size = 'md',
  title,
  dimWhenOff = false,
}: SwitchProps) {
  return (
    <span
      title={title}
      style={{
        display: 'inline-flex',
        alignItems: 'center',
        gap: 6,
        fontFamily: 'var(--font-mono)',
        fontSize: 11,
        color: 'var(--ink-dim)',
        opacity: dimWhenOff && !checked ? 0.55 : 1,
        transition: 'opacity 140ms ease',
      }}
    >
      <GoldToggle
        checked={checked}
        onChange={onChange}
        disabled={disabled}
        size={size}
        ariaLabel={ariaLabel ?? (typeof label === 'string' ? label : undefined)}
      />
      {label != null && (
        <span
          onClick={() => {
            if (!disabled) onChange(!checked)
          }}
          style={{ cursor: disabled ? 'not-allowed' : 'pointer', userSelect: 'none' }}
        >
          {label}
        </span>
      )}
    </span>
  )
}
