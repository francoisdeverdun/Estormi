/**
 * ManageHeader — the icon + eyebrow + name + status pill + summary row at the
 * top of SourceManageModal. Extracted from SourceManageModal.tsx.
 */
import { Fleuron } from '@estormi/ui-kit'
import { BrandIcon } from '../BrandIcon'
import type { SourceRowDescriptor, SourceConnection } from '../SourceRow'

const CONN_LABEL: Record<SourceConnection, { color: string; label: string }> = {
  connected: { color: 'var(--vert-sauge)', label: 'Connected' },
  failed: { color: 'var(--rouge-clair)', label: 'Failed' },
  'awaiting-scan': { color: 'var(--or-clair)', label: 'Awaiting scan' },
  disabled: { color: 'var(--ink-dimmer)', label: 'Disabled' },
  unknown: { color: 'var(--gilt-line)', label: 'Unknown' },
}

export function ManageHeader({
  desc,
  connection,
  chunks,
  watermark,
}: {
  desc: SourceRowDescriptor
  connection: SourceConnection
  chunks: number
  watermark: string
}) {
  const conn = CONN_LABEL[connection]
  return (
    <div
      style={{
        padding: '14px 28px 18px',
        borderBottom: '1px solid var(--gilt-line)',
        display: 'flex',
        alignItems: 'center',
        gap: 16,
      }}
    >
      <BrandIcon source={desc.key} size={48} />
      <div style={{ flex: 1, minWidth: 0 }}>
        <div
          style={{
            fontFamily: 'var(--font-display)',
            fontSize: 11,
            letterSpacing: '0.28em',
            color: 'var(--or-ancien)',
            textTransform: 'uppercase',
            marginBottom: 4,
            display: 'flex',
            alignItems: 'center',
            gap: 8,
          }}
        >
          <Fleuron size={7} /> Source · Manage
        </div>
        <h2
          id="source-manage-title"
          style={{
            fontFamily: 'var(--font-display)',
            fontSize: 26,
            fontWeight: 700,
            color: 'var(--parchemin)',
            letterSpacing: '0.04em',
            margin: 0,
          }}
        >
          {desc.label}
        </h2>
        <div style={{ display: 'flex', gap: 14, marginTop: 6, flexWrap: 'wrap' }}>
          <span
            style={{
              display: 'inline-flex',
              alignItems: 'center',
              gap: 6,
              fontSize: 13,
              color: conn.color,
              fontFamily: 'var(--font-ui)',
            }}
          >
            <span
              style={{
                width: 7,
                height: 7,
                borderRadius: '50%',
                background: conn.color,
                boxShadow: `0 0 6px ${conn.color}`,
              }}
            />
            {conn.label}
          </span>
          <span
            style={{
              fontFamily: 'var(--font-mono)',
              fontSize: 12,
              color: 'var(--ink-dim)',
            }}
          >
            {chunks.toLocaleString('en-US')} chunks · watermark {watermark || '—'}
          </span>
        </div>
      </div>
    </div>
  )
}
