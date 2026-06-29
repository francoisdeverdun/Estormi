/**
 * VaultTerminus — the procession's destination, the memory vault. Not a real
 * DAG stage and not selectable: it caps the rail with a fleuron and the run's
 * total yield, so "sources → vault" reads as a single flow. Extracted from
 * StageProcession.tsx.
 */
import { Fleuron } from '@estormi/ui-kit'
import { STATION } from './shared'

export function VaultTerminus({ reached, chunks }: { reached: boolean; chunks: number }) {
  const c = reached ? 'var(--or-ancien)' : 'var(--gilt-line-strong)'
  return (
    <div
      title={reached ? `Vault · ${chunks.toLocaleString()} chunks gathered` : 'Vault · awaiting the procession'}
      style={{
        display: 'flex',
        flexDirection: 'column',
        alignItems: 'center',
        flex: '1 1 0',
        minWidth: 0,
        gap: 3,
        padding: '0 1px 1px',
      }}
    >
      <span style={{ position: 'relative', width: '100%', height: STATION.rail, flexShrink: 0 }}>
        <span style={{ position: 'absolute', left: 0, width: '50%', top: '50%', height: 2, transform: 'translateY(-50%)', background: reached ? 'var(--or-ancien)' : 'var(--gilt-line)' }} />
        <span
          style={{
            position: 'absolute',
            left: '50%',
            top: '50%',
            width: STATION.disc,
            height: STATION.disc,
            transform: 'translate(-50%, -50%)',
            borderRadius: '50%',
            border: `2px solid ${c}`,
            background: 'var(--charbon)',
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
          }}
        >
          <Fleuron size={14} color={c} />
        </span>
      </span>
      <span
        style={{
          fontFamily: 'var(--font-display)',
          fontSize: 8.5,
          letterSpacing: '0.1em',
          textTransform: 'uppercase',
          color: reached ? 'var(--or-clair)' : 'var(--ink-dim)',
        }}
      >
        Vault
      </span>
      <span style={{ fontFamily: 'var(--font-mono)', fontSize: 8, lineHeight: 1, minHeight: 9, color: 'var(--or-clair)' }}>
        {reached && chunks > 0 ? `+${chunks.toLocaleString()}` : ''}
      </span>
    </div>
  )
}
