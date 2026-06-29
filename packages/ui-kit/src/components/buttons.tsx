/**
 * Buttons — Estormi's two canonical action shapes.
 *
 * PrimaryAction: filled gold leaf — the single most important action of a page
 * or panel (e.g. "Run", "Distill", "Save"). The gilt fill marks it as *the*
 * action to run; red is never a fill, only a destructive outline. There is at
 * most one PrimaryAction per surface.
 *
 * GhostAction: outlined gilt-line, used for secondary actions and toggles. An
 * `active` prop renders the active/pressed state without requiring hover.
 * `tone="danger"` is the red contour for destructive actions (delete / reset).
 */
import React from 'react'

type Size = 'sm' | 'md'

export interface PrimaryActionProps {
  label: string
  icon?: React.ReactNode
  onClick?: () => void
  size?: Size
  disabled?: boolean
  type?: 'button' | 'submit'
}

export function PrimaryAction({
  label,
  icon,
  onClick,
  size = 'md',
  disabled = false,
  type = 'button',
}: PrimaryActionProps) {
  const [hover, setHover] = React.useState(false)
  const pad = size === 'sm' ? '8px 14px' : '11px 20px'
  const fs = size === 'sm' ? 10 : 11

  return (
    <button
      type={type}
      onClick={onClick}
      disabled={disabled}
      onMouseEnter={() => setHover(true)}
      onMouseLeave={() => setHover(false)}
      style={{
        padding: pad,
        background: disabled ? 'var(--charbon-3)' : hover ? 'var(--or-clair)' : 'var(--or-ancien)',
        border: `1px solid ${disabled ? 'var(--gilt-line)' : 'var(--or-sombre)'}`,
        borderRadius: 'var(--radius-tight)',
        // Dark ink on gold — the gilt fill is light, so the label is the page
        // ground (not parchemin) for a crisp, high-contrast hero.
        color: disabled ? 'var(--ink-dimmer)' : 'var(--encre)',
        fontFamily: 'var(--font-display)',
        fontSize: fs,
        letterSpacing: '0.2em',
        textTransform: 'uppercase',
        fontWeight: 600,
        transition: 'all 140ms ease',
        display: 'inline-flex',
        alignItems: 'center',
        gap: 8,
        cursor: disabled ? 'not-allowed' : 'pointer',
      }}
    >
      {icon}
      {label}
    </button>
  )
}

export interface GhostActionProps {
  label: string
  icon?: React.ReactNode
  onClick?: () => void
  size?: Size
  active?: boolean
  disabled?: boolean
  /** 'danger' renders a destructive (red) action, e.g. Delete. */
  tone?: 'default' | 'danger'
}

export function GhostAction({
  label,
  icon,
  onClick,
  size = 'md',
  active = false,
  disabled = false,
  tone = 'default',
}: GhostActionProps) {
  const [hover, setHover] = React.useState(false)
  const pad = size === 'sm' ? '7px 12px' : '10px 18px'
  const fs = size === 'sm' ? 10 : 11
  const on = (hover || active) && !disabled
  const danger = tone === 'danger'
  const accent = danger ? 'var(--pourpre-clair)' : 'var(--or-ancien)'

  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      onMouseEnter={() => setHover(true)}
      onMouseLeave={() => setHover(false)}
      style={{
        padding: pad,
        background: active ? 'rgba(200,164,103,0.08)' : 'transparent',
        border: `1px solid ${on ? accent : danger ? 'var(--pourpre-clair)' : 'var(--gilt-line)'}`,
        borderRadius: 'var(--radius-tight)',
        color: disabled
          ? 'var(--ink-dimmer)'
          : danger
            ? 'var(--pourpre-clair)'
            : on
              ? 'var(--parchemin)'
              : 'var(--ink-dim)',
        fontFamily: 'var(--font-display)',
        fontSize: fs,
        letterSpacing: '0.2em',
        textTransform: 'uppercase',
        fontWeight: 500,
        transition: 'all 140ms ease',
        display: 'inline-flex',
        alignItems: 'center',
        gap: 6,
        cursor: disabled ? 'not-allowed' : 'pointer',
      }}
    >
      {icon}
      {label}
    </button>
  )
}
