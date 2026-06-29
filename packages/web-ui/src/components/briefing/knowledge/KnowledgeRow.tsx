/**
 * KnowledgeRow — one source row in the Diarium list: leading enable toggle, a
 * clickable body that opens the inline edit accordion, and a Remove action.
 * Extracted from KnowledgeSourcesPanel.tsx.
 */
import { GhostAction, GoldToggle } from '@estormi/ui-kit'
import type { KnowledgeSource } from '../../../api/settings'
import { EditSourceForm } from './EditSourceForm'

interface KnowledgeRowProps {
  source: KnowledgeSource
  expanded: boolean
  /** Click on the row body — opens / closes the inline edit form. */
  onToggle: () => void
  /** Click on the leading GoldToggle — flips `enabled` on the source. */
  onToggleEnabled: () => void
  onRemove: () => void
  onSave: (patch: KnowledgeSource) => void
  onCancel: () => void
}

export function KnowledgeRow({
  source,
  expanded,
  onToggle,
  onToggleEnabled,
  onRemove,
  onSave,
  onCancel,
}: KnowledgeRowProps) {
  const subtitle =
    source.type === 'rss'
      ? `RSS · ${(source.urls ?? []).length} URL${(source.urls ?? []).length === 1 ? '' : 's'}`
      : (source.url ?? '—')
  // `enabled` defaults to true when undefined so existing YAML rows
  // (which never had the field) stay ingested.
  const enabled = source.enabled !== false
  return (
    <div
      style={{
        background: 'var(--well-faint)',
        border: '1px solid var(--gilt-line)',
        display: 'grid',
        gridTemplateColumns: 'auto 1fr auto',
        alignItems: 'stretch',
        opacity: enabled ? 1 : 0.55,
        transition: 'opacity 200ms ease',
      }}
    >
      {/* Leading toggle — sibling of the body button (a <button> cannot
          contain another button). */}
      <div
        style={{
          display: 'flex',
          alignItems: 'center',
          paddingLeft: 12,
        }}
      >
        <GoldToggle
          checked={enabled}
          onChange={onToggleEnabled}
          ariaLabel={`Toggle ${source.label ?? source.id ?? 'source'}`}
          size="sm"
        />
      </div>
      <button
        type="button"
        onClick={onToggle}
        style={{
          display: 'grid',
          gridTemplateColumns: '1fr auto',
          alignItems: 'center',
          gap: 12,
          padding: '10px 14px',
          cursor: 'pointer',
          background: 'transparent',
          border: 'none',
          textAlign: 'left',
          font: 'inherit',
          color: 'inherit',
          width: '100%',
        }}
      >
        <div style={{ minWidth: 0 }}>
          <div
            style={{
              fontFamily: 'var(--font-display)',
              fontSize: 13,
              letterSpacing: '0.18em',
              color: 'var(--parchemin)',
              textTransform: 'uppercase',
              textDecoration: enabled ? 'none' : 'line-through',
            }}
          >
            {source.label ?? source.id ?? '—'}
          </div>
          <div
            style={{
              fontFamily: 'var(--font-mono)',
              fontSize: 12,
              color: 'var(--ink-dimmer)',
              overflow: 'hidden',
              textOverflow: 'ellipsis',
              whiteSpace: 'nowrap',
            }}
          >
            {subtitle}
          </div>
        </div>
        <span
          style={{
            fontFamily: 'var(--font-mono)',
            fontSize: 12,
            padding: '2px 8px',
            color: 'var(--or-ancien)',
            border: '1px solid var(--gilt-line)',
            textTransform: 'uppercase',
          }}
          title={`${source.mode ?? '—'} / ${source.axis ?? '—'}`}
        >
          {source.axis ?? '—'}
        </span>
      </button>
      <div style={{ display: 'flex', alignItems: 'center', paddingRight: 14 }}>
        <GhostAction label="Remove" size="sm" onClick={onRemove} />
      </div>
      {expanded && (
        <div style={{ gridColumn: '1 / -1' }}>
          <EditSourceForm source={source} onSave={onSave} onCancel={onCancel} />
        </div>
      )}
    </div>
  )
}
