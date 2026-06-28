/**
 * SourceRow — one line of the merged Sources panel.
 *
 * Combines the old "toggle + manage" surface with the per-source DAG row:
 *
 *   [toggle] [name + status / duration / history dots] [chunks] [▷|■] [⋮]
 *
 * Everything about a single source — enable, run, watch, manage — lives
 * on one line. The standalone DagSection is gone.
 *
 * Style rules (project-wide):
 *   - ■ stops a running thing
 *   - × removes a queued thing
 *   - ▷ runs a single source
 */
import { GoldToggle } from '@estormi/ui-kit'
import { SourceKebab } from './SourceKebab'
import { compactWatermark } from '../lib/watermark'

export type SourceConnection =
  | 'connected'
  | 'failed'
  | 'awaiting-scan'
  | 'disabled'
  | 'unknown'

export type SourceRunStatus =
  | 'idle'
  | 'wait'
  | 'running'
  | 'ok'
  | 'fail'
  | 'skip'
  | 'cancelled'

export interface SourceHistoryEntry {
  ok: boolean
  historyIdx: number
  status: string
  /** Pre-formatted duration string for the tooltip (e.g. "3m 12s"). */
  duration?: string
  /** Duration in seconds — drives the bar height. */
  durationS?: number
}

export interface SourceRowDescriptor {
  /** Canonical source key (matches /api/sources/{name}/…). */
  key: string
  /** Pretty display label, e.g. "Apple Notes". */
  label: string
  /** Settings/overview key — typically the watermark/counts column. */
  dbKey: string
  /** Optional filesystem path (Documents, Source Code) — shown below name. */
  path?: string
  /** True when this source needs pairing/auth in the Manage modal. */
  needsPairing?: boolean
}

export interface SourceRowProps {
  desc: SourceRowDescriptor
  enabled: boolean
  connection: SourceConnection
  chunks: number
  watermark: string
  onToggle: () => void
  onAction: () => void

  /* DAG plumbing — all optional so the row stays usable in a pure-toggle
     context too. The parent (merged SourcesPanel) wires these. */
  runStatus?: SourceRunStatus
  /** Pre-formatted duration string for the running stage ("00:42"). */
  duration?: string
  /** Up to ~5 prior runs as { ok, historyIdx } — newest rightmost. */
  history?: SourceHistoryEntry[]
  /** True when the source is enabled but its filesystem root is missing
   *  (documents / code only). Disables the play button. */
  awaitingSetup?: boolean
  onRunOnly?: () => void
  /** ■ stops the running DAG. Only meaningful when runStatus === 'running'. */
  onStopAll?: () => void
  /** True when *any* engine run is in flight. The engine lock is run-scoped, so
   *  a single-source run would 409 — every play button is greyed out until the
   *  run finishes (the currently-running stage shows ■ instead). */
  runInProgress?: boolean
  /** Click on the name cell — opens the full source history modal
   *  (live status + run list + per-run log). */
  onOpenHistory?: () => void
}

const CONN_STATE: Record<
  Exclude<SourceConnection, 'disabled'>,
  { color: string; label: 'Connected' | 'Error' | 'Disabled' | 'Awaiting scan' }
> = {
  connected: { color: 'var(--vert-sauge)', label: 'Connected' },
  failed: { color: 'var(--rouge-clair)', label: 'Error' },
  // Not "Disabled": an awaiting-scan source IS enabled, it just needs setup
  // (WhatsApp QR / a folder pick). Labelling it "Disabled" hid that the user
  // had to act — show the actionable state in gold instead.
  'awaiting-scan': { color: 'var(--or-clair)', label: 'Awaiting scan' },
  unknown: { color: 'var(--ink-dimmer)', label: 'Disabled' },
}

// Sources whose progress is tracked via a side mechanism, not a re-walkable
// watermark. This only gates the Manage modal copy + reset button (e.g. gcal's
// "Reset sync token") — the row itself shows the real freshness date: gcal now
// stamps ingestion_watermarks with the time its sync token was last saved (see
// google_calendar/sync.py `_stamp_watermark`), so the row prefers that date and
// only falls back to this label before the first stamped run.
// WhatsApp is intentionally absent: it tracks a real timestamp watermark (the
// durable-log `whatsapp_log` key, aliased onto `whatsapp` by the overview
// endpoint), so its row shows the last-ingested time like the watermark sources.
export const WATERMARK_MECHANISM: Record<string, string> = {
  gcal: 'sync tokens',
}

const RUN_STATUS_DOT: Record<SourceRunStatus, string> = {
  idle: 'var(--ink-dimmer)',
  wait: 'var(--ink-dim)',
  running: 'var(--or-vif)',
  ok: 'var(--vert-sauge)',
  fail: 'var(--rouge-clair)',
  skip: 'var(--ink-dimmer)',
  cancelled: 'var(--ink-dim)',
}

export function SourceRow({
  desc,
  enabled,
  connection,
  chunks,
  watermark,
  onToggle,
  onAction,
  runStatus = 'idle',
  duration,
  history = [],
  awaitingSetup = false,
  onRunOnly,
  onStopAll,
  runInProgress = false,
  onOpenHistory,
}: SourceRowProps) {
  const effective: SourceConnection = enabled ? connection : 'disabled'
  const conn =
    effective === 'disabled'
      ? { color: 'var(--ink-dimmer)', label: 'Disabled' }
      : {
          color: CONN_STATE[effective].color,
          label: CONN_STATE[effective].label,
        }

  // Prefer the real watermark (for gcal: the date its sync token was last
  // saved); fall back to the mechanism label only until that first stamp lands.
  const watermarkHint = watermark || WATERMARK_MECHANISM[desc.key] || ''
  const isRunning = runStatus === 'running'

  // The status sub-line takes one of three shapes:
  //   - running: yellow dot + uppercase "RUN" + live duration
  //   - enabled, idle: green/red/grey dot + connection label
  //   - disabled: grey dot + "DISABLED"
  // History dots render to the right of the status sub-line.

  return (
    <div
      role="row"
      data-source={desc.key}
      style={{
        display: 'grid',
        // [toggle] [name+status] [watermark] [chunks] [run/stop] [kebab]
        // Fixed widths for watermark (115px) and chunks (52px) so the two
        // right-aligned values never butt up against each other; the
        // previous ``auto auto`` collapsed them into a single mashed block.
        // Bigger column gap (10px vs 6) gives each cell visual breathing
        // room without forcing the name column to shrink.
        gridTemplateColumns: '26px 1fr 115px 52px 22px 22px',
        alignItems: 'center',
        gap: 10,
        padding: '8px 10px',
        borderBottom: '1px solid color-mix(in srgb, var(--brass-mid) 10%, transparent)',
        background: isRunning
          ? 'color-mix(in srgb, var(--or-vif) 6%, transparent)'
          : 'transparent',
        opacity: enabled ? 1 : 0.55,
        transition: 'opacity 200ms ease, background 120ms ease',
      }}
    >
      <div>
        <GoldToggle
          checked={enabled}
          onChange={onToggle}
          ariaLabel={`Toggle ${desc.label}`}
          size="sm"
        />
      </div>

      <button
        type="button"
        onClick={onOpenHistory}
        disabled={!onOpenHistory}
        title={onOpenHistory ? `Open ${desc.label} history` : undefined}
        style={{
          minWidth: 0,
          background: 'transparent',
          border: 'none',
          padding: 0,
          textAlign: 'left',
          color: 'inherit',
          cursor: onOpenHistory ? 'pointer' : 'default',
          font: 'inherit',
        }}
      >
        <div
          style={{
            fontFamily: 'var(--font-ui)',
            fontSize: 13,
            fontWeight: 500,
            color: 'var(--parchemin)',
            textDecoration: enabled ? 'none' : 'line-through',
            whiteSpace: 'nowrap',
            overflow: 'hidden',
            textOverflow: 'ellipsis',
          }}
        >
          {desc.label}
        </div>
        <div
          style={{
            display: 'flex',
            alignItems: 'center',
            gap: 6,
            marginTop: 1,
            minWidth: 0,
          }}
        >
          <span
            aria-hidden="true"
            style={{
              width: 5,
              height: 5,
              background: isRunning ? RUN_STATUS_DOT.running : conn.color,
              borderRadius: '50%',
              flexShrink: 0,
              boxShadow:
                isRunning ||
                effective === 'connected' ||
                effective === 'failed'
                  ? `0 0 4px ${isRunning ? RUN_STATUS_DOT.running : conn.color}`
                  : 'none',
            }}
          />
          <span
            style={{
              fontFamily: 'var(--font-display)',
              fontSize: 9,
              letterSpacing: '0.18em',
              color: isRunning ? RUN_STATUS_DOT.running : conn.color,
              textTransform: 'uppercase',
              whiteSpace: 'nowrap',
              overflow: 'hidden',
              textOverflow: 'ellipsis',
            }}
            title={watermarkHint ? `${conn.label} · ${watermarkHint}` : conn.label}
          >
            {isRunning ? 'running' : awaitingSetup ? 'awaiting setup' : conn.label}
          </span>
          {isRunning && duration && (
            <span
              style={{
                fontFamily: 'var(--font-mono)',
                fontSize: 10,
                color: RUN_STATUS_DOT.running,
                letterSpacing: 0,
              }}
            >
              {duration}
            </span>
          )}
        </div>
      </button>

      {/* Watermark / freshness column — replaces the prior 5-run bar strip.
          ``watermarkHint`` is the source's last-fetched timestamp (set in
          SourcesPanel from ``overview.sources.watermarks[key]``); when
          missing we fall back to the most recent run's relative time so
          the column always shows SOMETHING useful at a glance.

          The raw watermark format varies wildly per source (32-char ISO
          for some, "sync tokens"/"live staging" for others); we compact
          ISO-shaped strings to ``MM-DD HH:MM`` so the column lines up
          visually across rows without truncating non-ISO fallbacks. */}
      <div
        style={{
          textAlign: 'right',
          fontFamily: 'var(--font-mono)',
          fontSize: 10,
          color: 'var(--ink-dim)',
          whiteSpace: 'nowrap',
          overflow: 'hidden',
          textOverflow: 'ellipsis',
        }}
        title={
          watermarkHint
            ? `Last fetched: ${watermarkHint}`
            : history.length
              ? `Last run #${history[history.length - 1]?.historyIdx ?? '?'}`
              : 'No watermark yet'
        }
      >
        {compactWatermark(
          watermarkHint || (history.length ? history[history.length - 1]?.duration ?? '—' : '—')
        )}
      </div>

      <div
        style={{
          fontFamily: 'var(--font-display)',
          fontSize: 14,
          fontWeight: 700,
          color: 'var(--or-clair)',
          letterSpacing: '0.02em',
          textAlign: 'right',
          whiteSpace: 'nowrap',
        }}
        title={`${chunks.toLocaleString('en-US')} chunks`}
      >
        {chunks.toLocaleString('en-US')}
      </div>

      {/* Run / stop affordance — ▷ runs this source only, ■ stops the whole
          DAG (the engine lock is run-scoped). Disabled or awaiting-setup
          sources show a dimmed ▷ that can't be clicked. */}
      {isRunning ? (
        <button
          type="button"
          onClick={(e) => {
            e.stopPropagation()
            onStopAll?.()
          }}
          disabled={!onStopAll}
          title="Stop the running DAG"
          aria-label="Stop"
          style={iconBtn(RUN_STATUS_DOT.running)}
        >
          ■
        </button>
      ) : (
        <button
          type="button"
          onClick={(e) => {
            e.stopPropagation()
            onRunOnly?.()
          }}
          disabled={!onRunOnly || !enabled || awaitingSetup || runInProgress}
          title={
            !enabled
              ? 'Enable the source first'
              : awaitingSetup
                ? 'Set this source up via Manage first'
                : runInProgress
                  ? 'A run is already in progress'
                  : 'Run this source only'
          }
          aria-label="Run this source"
          style={playBtn(!!onRunOnly && enabled && !awaitingSetup && !runInProgress)}
        >
          ▶
        </button>
      )}

      <div style={{ display: 'flex', justifyContent: 'flex-end' }}>
        <SourceKebab sourceLabel={desc.label} onSelect={onAction} />
      </div>
    </div>
  )
}

const iconBtn = (color: string, disabled = false): React.CSSProperties => ({
  width: 22,
  height: 22,
  display: 'inline-flex',
  alignItems: 'center',
  justifyContent: 'center',
  background: 'transparent',
  border: '1px solid var(--gilt-line)',
  color,
  fontFamily: 'var(--font-mono)',
  fontSize: 11,
  lineHeight: 1,
  cursor: disabled ? 'not-allowed' : 'pointer',
  padding: 0,
  opacity: disabled ? 0.5 : 1,
})

// The play button is a filled-gold hero like ui-kit's PrimaryAction (same
// tokens): dark glyph on a gilt fill when runnable, a flat charbon chip when
// not. Distinct from `iconBtn` (the outlined ■ stop) so "run" reads as the
// affirmative action and a greyed play clearly can't be clicked.
const playBtn = (enabled: boolean): React.CSSProperties => ({
  width: 22,
  height: 22,
  display: 'inline-flex',
  alignItems: 'center',
  justifyContent: 'center',
  background: enabled ? 'var(--or-ancien)' : 'var(--charbon-3)',
  border: `1px solid ${enabled ? 'var(--or-sombre)' : 'var(--gilt-line)'}`,
  color: enabled ? 'var(--encre)' : 'var(--ink-dimmer)',
  fontFamily: 'var(--font-mono)',
  fontSize: 10,
  lineHeight: 1,
  cursor: enabled ? 'pointer' : 'not-allowed',
  padding: 0,
  transition: 'all 140ms ease',
})
