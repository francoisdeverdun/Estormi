/**
 * SectionHeader — section title row.
 *
 * Eyebrow line (Latin term flanked by fleurons), optional blocked lettrine
 * (the rounded burgundy initial — the same device as the iOS masthead),
 * gradient gold H2, optional subtitle, and a slot for an action button on
 * the right. Used as the header of each major content block on every page.
 */
import React from 'react'
import { Fleuron } from './marks'
import { EstormiLogoMark } from './LogoMark'

export interface SectionHeaderProps {
  eyebrow?: string
  title: string
  subtitle?: string
  action?: React.ReactNode
  letter?: string
}

export function SectionHeader({
  eyebrow,
  title,
  subtitle,
  action,
  letter,
}: SectionHeaderProps) {
  const integrated =
    letter && title && title[0]?.toUpperCase() === letter.toUpperCase()
  const rest = integrated ? title.slice(1) : title

  return (
    <div
      style={{
        display: 'flex',
        alignItems: 'flex-end',
        justifyContent: 'space-between',
        // Allow the action chip to drop below the title at narrow widths
        // instead of overlapping the H2 (which can't shrink past the cap +
        // text content). Pairs with `flex-shrink: 0` on the action wrapper.
        flexWrap: 'wrap',
        marginBottom: 18,
        gap: 20,
        // Query container so the H2 can scale to the panel it sits in —
        // a long title (e.g. "Latest briefing") in a narrow side panel
        // would otherwise overflow at the fixed 28px size.
        containerType: 'inline-size',
      }}
    >
      <div
        style={{
          display: 'flex',
          flexDirection: 'column',
          // Natural width (no `flex: 1`): the parent flex-wraps the action
          // chip onto its own line when space is tight, instead of crushing
          // the title block. The H2 is kept from overflowing a narrow panel
          // by the container-query font scaling below — not by shrinking
          // this block.
          flex: '0 0 auto',
          minWidth: 0,
          maxWidth: '100%',
        }}
      >
        {eyebrow && (
          <div
            style={{
              fontFamily: 'var(--font-display)',
              fontSize: 12,
              letterSpacing: '0.32em',
              color: 'var(--or-ancien)',
              marginBottom: 8,
              fontWeight: 500,
              display: 'flex',
              alignItems: 'center',
              gap: 8,
              textTransform: 'uppercase',
            }}
          >
            <Fleuron size={8} color="var(--or-ancien)" />
            {eyebrow}
            <Fleuron size={8} color="var(--or-ancien)" />
          </div>
        )}
        <div
          style={{
            display: 'flex',
            alignItems: 'center',
            gap: integrated ? 6 : 16,
          }}
        >
          {letter && (
            <EstormiLogoMark letter={letter} size={48} seed={letter.charCodeAt(0)} />
          )}
          <h2
            style={{
              fontFamily: 'var(--font-display)',
              fontWeight: 700,
              // Scales with the SectionHeader's own width (cqi) so the
              // title fits narrow side panels and stays 28px on wide ones.
              // Floor is low enough that the longest word still fits beside
              // the 56px cap in the app's narrowest panels (~150px wide).
              fontSize: 'clamp(12px, 7.5cqi, 28px)',
              letterSpacing: '0.04em',
              color: 'transparent',
              background: 'var(--gilt-gradient)',
              WebkitBackgroundClip: 'text',
              backgroundClip: 'text',
              WebkitTextStroke: '0.3px rgba(0,0,0,0.4)',
              textTransform: 'uppercase',
              lineHeight: 1.05,
              margin: 0,
            }}
          >
            {rest}
          </h2>
        </div>
        {subtitle && (
          <p
            style={{
              fontFamily: 'var(--font-ui)',
              fontSize: 16,
              color: 'var(--ink-dim)',
              marginTop: 8,
            }}
          >
            {subtitle}
          </p>
        )}
      </div>
      {action && (
        // `flex-shrink: 0` keeps the chip at its natural width and lets the
        // title shrink first; on very narrow widths `flex-wrap` on the
        // parent drops it to a new line instead of crashing into the H2.
        <div style={{ flexShrink: 0, alignSelf: 'flex-end' }}>{action}</div>
      )}
    </div>
  )
}
