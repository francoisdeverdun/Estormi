/**
 * GildedPanel — the canonical Estormi card.
 *
 * Rounded charbon panel inside a single gilt hairline — the same card frame
 * as the iOS GildedPanel (gold hairline on a rounded continuous-corner
 * panel). `gold` strengthens the frame and lays a faint gold wash for hero /
 * featured cards. All major content blocks (lists, tables, dashboards,
 * briefing items) sit inside one of these.
 */
import React from 'react'

export interface GildedPanelProps {
  children: React.ReactNode
  /** Stronger gilt frame + faint gold wash (for hero / featured cards). */
  gold?: boolean
  /** Pad the inner content (default `true`). */
  padded?: boolean
  style?: React.CSSProperties
  className?: string
}

export function GildedPanel({
  children,
  gold = false,
  padded = true,
  style,
  className,
}: GildedPanelProps) {
  return (
    <div
      className={className}
      style={{
        backgroundColor: 'var(--charbon)',
        // Faint gold wash layered over the charbon ground on hero cards
        // (longhand, not the `background` shorthand — jsdom drops gradient
        // layers from the shorthand, and the longhands are equivalent).
        backgroundImage: gold
          ? 'linear-gradient(var(--overlay-gilt), var(--overlay-gilt))'
          : undefined,
        border: gold
          ? '1px solid var(--gilt-line-strong)'
          : '1px solid var(--gilt-line)',
        borderRadius: 'var(--radius-panel)',
        padding: padded ? '22px 24px' : 0,
        position: 'relative',
        ...style,
      }}
    >
      {children}
    </div>
  )
}
