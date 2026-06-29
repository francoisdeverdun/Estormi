/**
 * CardinalSection — top-of-page cardinal counters, clickable.
 *
 * Layout (single column, ~440–520 px wide):
 *   1. Slim title row — illuminated S + "Summarium" wordmark.
 *   2. Chunks tile (full width) — total chunks in the vault.
 *   3. Briefings counter — opens the briefing modal.
 *   4. Compact 14-day pulse.
 *
 * Each counter IS the trigger — no separate cards row; sources are
 * visible in Parameters as the source toggles.
 */
import { useEffect } from 'react'
import { fmtInt } from '../lib/format'
import {
  EstormiLogoMark,
  Fleuron,
  GildedPanel,
  IlluminatedRule,
} from '@estormi/ui-kit'
import { MemoriaPulse } from '../components/MemoriaPulse'
import { type Overview } from '../api/overview'
import { listBriefings } from '../api/knowledge'
import { useSnapshotState } from '../state/snapshotCache'
import { useOverviewPoll } from '../state/useOverviewPoll'
import { useSettings } from '../hooks/useSettings'

export interface CardinalSectionProps {
  onOpenBriefing: () => void
  /** Open the Character (About-you) modal. */
  onOpenCharacter: () => void
  /** Bumped each time the Character modal closes, so the tile re-reads the
   *  profile and its preview stays fresh after an edit. */
  characterRev: number
}

export function CardinalSection({
  onOpenBriefing,
  onOpenCharacter,
  characterRev,
}: CardinalSectionProps) {
  const [overview] = useSnapshotState<Overview | null>('overview', null)
  const [briefingsTotal, setBriefingsTotal] = useSnapshotState<number>(
    'cardinal.briefingsTotal',
    0,
  )

  // The overview snapshot is driven by the shared poller; here we only poll the
  // briefings count.
  useOverviewPoll()
  useEffect(() => {
    let cancelled = false
    const tick = async () => {
      try {
        const br = await listBriefings()
        if (!cancelled) setBriefingsTotal(br.items?.length ?? 0)
      } catch {
        /* silent — retried on the next tick */
      }
    }
    void tick()
    // The count changes at most once per scheduled briefing, so a 30s cadence
    // is ample — /api/briefings enumerates and reads the whole archive, so a 5s
    // poll re-reads it 6× more often than the data can change.
    const id = window.setInterval(tick, 30_000)
    return () => {
      cancelled = true
      window.clearInterval(id)
    }
  }, [setBriefingsTotal])

  const totalChunks = overview?.storage?.total_chunks ?? null

  return (
    <GildedPanel gold>
      {/* Title row — matches the Sources panel pattern: small Latin eyebrow
          between fleurons, then a large illuminated cap + gold-gradient
          word. Reads as "Summarium" with the "S" as the lettrine. */}
      <div style={{ marginTop: 6, marginBottom: 12 }}>
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
          {"Ars Memoriae"}
          <Fleuron size={8} color="var(--or-ancien)" />
        </div>
        <div
          style={{
            display: 'flex',
            alignItems: 'center',
            gap: 8,
          }}
        >
          <EstormiLogoMark letter="S" size={42} seed={83} />
          <h2
            style={{
              fontFamily: 'var(--font-display)',
              fontWeight: 700,
              fontSize: 24,
              letterSpacing: '0.04em',
              color: 'transparent',
              background: 'var(--gilt-gradient)',
              WebkitBackgroundClip: 'text',
              backgroundClip: 'text',
              WebkitTextStroke: '0.3px var(--shadow-faint)',
              textTransform: 'uppercase',
              lineHeight: 1,
              margin: 0,
            }}
          >
            {"ummarium"}
          </h2>
        </div>
      </div>

      <IlluminatedRule />

      {/* About you — your "character": the profile fed to the quills on every
          run. Top-of-page and prominent; opens the live-edit modal. */}
      <div style={{ marginTop: 12 }}>
        <CharacterTile onOpen={onOpenCharacter} rev={characterRev} />
      </div>

      {/* Chunks tile — full width. Read-only: the vault total. Not clickable
          — there is no Chunks modal; deletion is per-source (Manage a source →
          Reset data). */}
      <div
        style={{
          marginTop: 12,
          width: '100%',
          padding: '12px 14px',
          border: '1px solid var(--gilt-line)',
          borderRadius: 'var(--radius-tight)',
          background: 'var(--well-faint)',
          display: 'flex',
          alignItems: 'center',
          gap: 16,
          textAlign: 'left',
        }}
      >
        <div style={{ flex: 1, minWidth: 0 }}>
          <div style={tileLabel}>
            {"Chunks"}
          </div>
          <div style={tileValue}>{fmtInt(totalChunks)}</div>
          <div style={tileSub}>
            {"in the vault"}
          </div>
        </div>
      </div>

      {/* Clickable counter — opens the briefing modal */}
      <div style={{ marginTop: 8 }}>
        <CounterTile
          label={"Briefings"}
          value={briefingsTotal}
          accent="var(--vert-sauge)"
          onClick={onOpenBriefing}
        />
      </div>

      {/* Graph — always open (the chart is always relevant in the
          one-pager). Plots the cumulative memory store so the stack climbs
          over time, matching the iOS companion's Memoria card. */}
      <div style={{ marginTop: 14 }}>
        <MemoriaPulse
          eyebrowOverride={"Memoria"}
          titleOverride={"14 days · by source"}
        />
      </div>

      {/* Closing flourish — the minimal illuminated rule, echoing the
          masthead and the iOS "geometry and air" aesthetic. */}
      <div style={{ marginTop: 16 }}>
        <IlluminatedRule />
      </div>
    </GildedPanel>
  )
}

function CounterTile({
  label,
  value,
  accent,
  onClick,
}: {
  label: string
  value: number
  accent: string
  onClick: () => void
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      style={{
        width: '100%',
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'space-between',
        gap: 10,
        padding: '10px 14px',
        background: 'var(--well-faint)',
        border: '1px solid var(--gilt-line)',
        borderLeft: `3px solid ${accent}`,
        borderRadius: 'var(--radius-tight)',
        color: 'inherit',
        textAlign: 'left',
        cursor: 'pointer',
        fontFamily: 'inherit',
        transition: 'background 120ms ease, border-color 120ms ease',
      }}
      onMouseEnter={(e) => {
        ;(e.currentTarget as HTMLButtonElement).style.background =
          'var(--overlay-gilt)'
        ;(e.currentTarget as HTMLButtonElement).style.borderColor = accent
      }}
      onMouseLeave={(e) => {
        const el = e.currentTarget as HTMLButtonElement
        el.style.background = 'var(--well-faint)'
        el.style.borderColor = 'var(--gilt-line)'
        // Restore the resting accent on the left edge — `borderColor` above
        // (and the hover handler) recolour all four sides, so without this the
        // accent stripe is wiped after the first hover.
        el.style.borderLeftColor = accent
      }}
      aria-label={`${label} (${fmtInt(value)})`}
    >
      <div style={{ minWidth: 0 }}>
        <div style={tileLabel}>{label}</div>
        <div
          style={{
            fontFamily: 'var(--font-display)',
            fontSize: 22,
            color: 'var(--parchemin)',
            fontWeight: 700,
            lineHeight: 1.05,
            marginTop: 2,
          }}
        >
          {fmtInt(value)}
        </div>
      </div>
      <span
        aria-hidden="true"
        style={{
          fontFamily: 'var(--font-mono)',
          fontSize: 13,
          color: accent,
        }}
      >
        →
      </span>
    </button>
  )
}

function CharacterTile({ onOpen, rev }: { onOpen: () => void; rev: number }) {
  const { settings, refresh } = useSettings()
  // Re-read the profile whenever the modal closes (rev bumps) so the preview
  // reflects a just-made edit without a full page reload.
  useEffect(() => {
    void refresh()
  }, [rev, refresh])

  const text = ((settings ?? {})['briefing_user_context'] || '').replace(/\s+/g, ' ').trim()
  const hasText = text.length > 0
  const accent = 'var(--or-ancien)'

  return (
    <button
      type="button"
      onClick={onOpen}
      style={{
        width: '100%',
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'space-between',
        gap: 10,
        padding: '12px 14px',
        background: 'var(--well-faint)',
        border: '1px solid var(--gilt-line)',
        borderLeft: `3px solid ${accent}`,
        borderRadius: 'var(--radius-tight)',
        color: 'inherit',
        textAlign: 'left',
        cursor: 'pointer',
        fontFamily: 'inherit',
        transition: 'background 120ms ease, border-color 120ms ease',
      }}
      onMouseEnter={(e) => {
        ;(e.currentTarget as HTMLButtonElement).style.background = 'var(--overlay-gilt)'
        ;(e.currentTarget as HTMLButtonElement).style.borderColor = accent
        ;(e.currentTarget as HTMLButtonElement).style.borderLeftColor = accent
      }}
      onMouseLeave={(e) => {
        const el = e.currentTarget as HTMLButtonElement
        el.style.background = 'var(--well-faint)'
        el.style.borderColor = 'var(--gilt-line)'
        el.style.borderLeftColor = accent
      }}
      aria-label="About you — your character (opens editor)"
    >
      <div style={{ minWidth: 0, flex: 1 }}>
        <div style={tileLabel}>{'About you'}</div>
        <div
          style={{
            fontFamily: 'var(--font-body)',
            fontSize: 13,
            color: hasText ? 'var(--parchemin)' : 'var(--ink-dim)',
            fontStyle: hasText ? 'normal' : 'italic',
            lineHeight: 1.4,
            marginTop: 3,
            display: '-webkit-box',
            WebkitLineClamp: 2,
            WebkitBoxOrient: 'vertical',
            overflow: 'hidden',
          }}
        >
          {hasText ? text : 'Tell Estormi who you are — tap to set up your character.'}
        </div>
      </div>
      <span aria-hidden="true" style={{ fontFamily: 'var(--font-mono)', fontSize: 13, color: accent }}>
        →
      </span>
    </button>
  )
}

/* ─────────────────── styles ─────────────────── */

const tileLabel = {
  fontFamily: 'var(--font-display)',
  fontSize: 9,
  letterSpacing: '0.22em',
  color: 'var(--or-ancien)',
  textTransform: 'uppercase' as const,
}

const tileValue = {
  fontFamily: 'var(--font-display)',
  fontSize: 26,
  color: 'var(--parchemin)',
  fontWeight: 700,
  lineHeight: 1.05,
  marginTop: 2,
}

const tileSub = {
  fontFamily: 'var(--font-mono)',
  fontSize: 11,
  color: 'var(--ink-dim)',
  marginTop: 2,
}
