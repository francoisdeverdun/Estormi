/**
 * Shared geometry + status helpers for the ingestion "procession" — the
 * horizontal frieze of stage stations terminating at the vault. Used by
 * IngestionStageBody and its station sub-components (ProcessionNode,
 * VaultTerminus). Extracted from StageProcession.tsx.
 */
import { SOURCES } from '../../SourcesPanel'

const STAGE_LABEL: Record<string, string> = Object.fromEntries(
  SOURCES.map((s) => [s.key, s.label]),
)

export const stageLabel = (name: string): string =>
  STAGE_LABEL[name] ?? name.replace(/_/g, ' ')

export const TERMINAL_STATUSES = new Set(['ok', 'fail', 'skip', 'cancelled'])

export function stageVisual(status: string): {
  color: string
  glyph: string
  live: boolean
} {
  switch (status) {
    case 'ok':
      return { color: 'var(--vert-sauge)', glyph: '✓', live: false }
    case 'fail':
      return { color: 'var(--rouge-clair)', glyph: '✕', live: false }
    case 'running':
      return { color: 'var(--pourpre-clair)', glyph: '◆', live: true }
    case 'skip':
      return { color: 'var(--ink-dim)', glyph: '⊘', live: false }
    case 'cancelled':
      return { color: 'var(--or-ancien)', glyph: '⊗', live: false }
    default: // pending / wait
      return { color: 'var(--gilt-line-strong)', glyph: '·', live: false }
  }
}

export const fmtClock = (secs: number): string =>
  `${String(Math.floor(secs / 60)).padStart(2, '0')}:${String(secs % 60).padStart(2, '0')}`

// Shared geometry for a horizontal frieze station so the gilt rail lines up
// across nodes and the vault terminus. Columns flex-shrink to fit the modal's
// width (≈472px in the 520px app window) so all stations stay on one row with
// no horizontal scroll.
export const STATION = { disc: 26, rail: 28 } as const
