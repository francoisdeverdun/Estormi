/**
 * StorageBar — proportional segmented bar with legend.
 *
 * Used in the Ingestion hero "Memoria" strip to show how the on-disk
 * vault is split between SQLite, Qdrant, the staging dir and the cache.
 * Each segment's width is proportional to its byte count; segments with
 * zero bytes are still listed in the legend so callers can see the
 * shape of the storage even on a fresh install.
 *
 * Page-specific composition — does not belong in ui-kit because it
 * encodes the four-segment storage taxonomy of the Estormi data
 * directory rather than a generic visual primitive.
 */
export interface StorageSegment {
  label: string
  /** Bytes — converted to MB for display. */
  bytes: number
  /** CSS colour (typically a `var(--...)` token). */
  color: string
}

export interface StorageBarProps {
  segments: StorageSegment[]
  /** Optional path label shown right-aligned next to the headline total. */
  path?: string
  /** Optional secondary line under the headline (e.g. on-disk total + free space). */
  detail?: React.ReactNode
  /** Bar height in px. */
  height?: number
}

const BYTES_PER_MB = 1024 * 1024

function toMB(bytes: number): number {
  return bytes / BYTES_PER_MB
}

function fmtMB(bytes: number): string {
  const mb = toMB(bytes)
  if (mb < 0.05) return '0.0'
  return mb.toFixed(1)
}

export function StorageBar({ segments, path, detail, height = 10 }: StorageBarProps) {
  const totalBytes = segments.reduce((s, x) => s + x.bytes, 0)
  const safeTotal = totalBytes > 0 ? totalBytes : 1 // avoid div-by-zero on empty vault

  return (
    <div style={{ display: 'flex', flexDirection: 'column', minWidth: 0 }}>
      <div style={{ display: 'flex', alignItems: 'baseline', gap: 6, marginBottom: 8 }}>
        <span
          style={{
            fontFamily: 'var(--font-display)',
            fontSize: 34,
            fontWeight: 700,
            color: 'var(--or-clair)',
            letterSpacing: '0.02em',
          }}
        >
          {fmtMB(totalBytes)}
        </span>
        <span
          style={{
            fontFamily: 'var(--font-display)',
            fontSize: 12,
            color: 'var(--or-ancien)',
            letterSpacing: '0.2em',
          }}
        >
          MB
        </span>
        {path && (
          <span
            style={{
              marginLeft: 'auto',
              fontFamily: 'var(--font-mono)',
              fontSize: 11,
              color: 'var(--ink-dim)',
              overflow: 'hidden',
              textOverflow: 'ellipsis',
              whiteSpace: 'nowrap',
              maxWidth: '60%',
            }}
            title={path}
          >
            {path}
          </span>
        )}
      </div>
      {detail && (
        <div
          style={{
            fontFamily: 'var(--font-mono)',
            fontSize: 11,
            color: 'var(--ink-dim)',
            marginTop: -2,
            marginBottom: 8,
          }}
        >
          {detail}
        </div>
      )}
      <div
        role="img"
        aria-label={`Storage breakdown: ${segments
          .map((s) => `${s.label} ${fmtMB(s.bytes)} MB`)
          .join(', ')}`}
        style={{
          display: 'flex',
          height,
          border: '1px solid var(--gilt-line-strong)',
          marginBottom: 10,
          overflow: 'hidden',
          background: 'var(--well-deep)',
        }}
      >
        {totalBytes > 0 &&
          segments.map((s) => (
            <div
              key={s.label}
              style={{
                width: `${(s.bytes / safeTotal) * 100}%`,
                background: s.color,
                opacity: 0.85,
                borderRight: '1px solid var(--encre)',
              }}
            />
          ))}
      </div>
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(2, 1fr)', gap: 4 }}>
        {segments.map((s) => (
          <div
            key={s.label}
            style={{ display: 'flex', alignItems: 'center', gap: 6, fontSize: 12 }}
          >
            <span
              style={{
                width: 7,
                height: 7,
                background: s.color,
                flexShrink: 0,
              }}
            />
            <span
              style={{
                fontFamily: 'var(--font-ui)',
                color: 'var(--ink-dim)',
                flex: 1,
              }}
            >
              {s.label}
            </span>
            <span
              style={{
                fontFamily: 'var(--font-mono)',
                color: 'var(--parchemin)',
              }}
            >
              {fmtMB(s.bytes)}
            </span>
          </div>
        ))}
      </div>
    </div>
  )
}
