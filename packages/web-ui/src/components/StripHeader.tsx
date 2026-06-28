/**
 * StripHeader — the eyebrow + title row rendered at the top of a
 * vine-bordered hero strip (e.g. the Ingestion pulse).
 */
import { Fleuron } from '@estormi/ui-kit'

export interface StripHeaderProps {
  eyebrow: string
  title: string
}

export function StripHeader({ eyebrow, title }: StripHeaderProps) {
  return (
    <div style={{ marginBottom: 12 }}>
      <div
        style={{
          fontFamily: 'var(--font-display)',
          fontSize: 10,
          letterSpacing: '0.28em',
          color: 'var(--or-ancien)',
          textTransform: 'uppercase',
          display: 'flex',
          alignItems: 'center',
          gap: 6,
          marginBottom: 4,
        }}
      >
        <Fleuron size={6} /> {eyebrow}
      </div>
      <div
        style={{
          fontFamily: 'var(--font-display)',
          fontSize: 16,
          fontWeight: 700,
          letterSpacing: '0.06em',
          color: 'var(--parchemin)',
          textTransform: 'uppercase',
        }}
      >
        {title}
      </div>
    </div>
  )
}
