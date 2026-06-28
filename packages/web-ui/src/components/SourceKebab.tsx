/**
 * SourceKebab — three-dot button that opens the source's Manage modal.
 *
 * Originally a popover with Reset watermark / Reset data / Manage. The
 * popover duplicated controls that already live inside Manage (Reset
 * watermark in the Watermark section, Reset data in the Danger zone),
 * so the menu was just slower indirection. Now a plain ``⋮`` button:
 * one click opens Manage.
 */
import { useState } from 'react'

export interface SourceKebabProps {
  /** Display label for screen readers. */
  sourceLabel: string
  onSelect: () => void
}

export function SourceKebab({ sourceLabel, onSelect }: SourceKebabProps) {
  const [hover, setHover] = useState(false)
  return (
    <button
      type="button"
      onClick={() => onSelect()}
      onMouseEnter={() => setHover(true)}
      onMouseLeave={() => setHover(false)}
      aria-label={`Manage source · ${sourceLabel}`}
      title={`Manage ${sourceLabel}`}
      style={{
        width: 26,
        height: 26,
        color: hover ? 'var(--or-clair)' : 'var(--ink-dim)',
        border: `1px solid ${hover ? 'var(--or-ancien)' : 'var(--gilt-line)'}`,
        background: hover ? 'var(--overlay-gilt-strong)' : 'transparent',
        fontSize: 19,
        lineHeight: 1,
        cursor: 'pointer',
        fontFamily: 'var(--font-display)',
        transition: 'color 140ms ease, border-color 140ms ease, background 140ms ease',
      }}
    >
      ⋮
    </button>
  )
}
