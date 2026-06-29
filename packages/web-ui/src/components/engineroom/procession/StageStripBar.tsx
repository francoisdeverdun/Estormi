/**
 * StageStripBar — the thin label bar between the stage strip and the log,
 * carrying the selected stage's name + status and the raw-log toggle. Extracted
 * from StageProcession.tsx.
 */
export function StageStripBar({
  color,
  raw,
  onToggleRaw,
  label,
  sub,
}: {
  color: string
  raw: boolean
  onToggleRaw: () => void
  label: string
  sub?: string
}) {
  return (
    <div
      style={{
        flex: '0 0 auto',
        padding: '6px 12px',
        display: 'flex',
        alignItems: 'baseline',
        gap: 8,
        borderBottom: '1px solid var(--gilt-line)',
        background: 'var(--charbon)',
      }}
    >
      <span
        style={{
          fontFamily: 'var(--font-mono)',
          fontSize: 11,
          color: color,
          fontWeight: 700,
        }}
      >
        {label}
      </span>
      {sub && (
        <span style={{ fontFamily: 'var(--font-mono)', fontSize: 11, color: 'var(--ink-dim)' }}>
          {sub}
        </span>
      )}
      <button
        type="button"
        onClick={onToggleRaw}
        style={{
          marginLeft: 'auto',
          padding: '2px 8px',
          background: 'transparent',
          border: '1px solid var(--gilt-line-strong)',
          color: raw ? color : 'var(--ink-dim)',
          fontFamily: 'var(--font-mono)',
          fontSize: 10,
          letterSpacing: '0.08em',
          cursor: 'pointer',
        }}
      >
        {raw ? '‹ stages' : 'raw log'}
      </button>
    </div>
  )
}
