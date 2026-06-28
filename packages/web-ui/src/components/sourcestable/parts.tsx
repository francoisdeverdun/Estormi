/**
 * Presentational sub-components for SourcesPanel — the "+ ATELIER + · MEMORIA"
 * header with its inline schedule editor, the 6-column table header, and the
 * post-activation macOS permission notice. Extracted from SourcesPanel.tsx so
 * the panel body stays focused on assembling rows.
 */
import { useEffect, useState } from 'react'
import { EstormiLogoMark, GhostAction, Switch, TextInput } from '@estormi/ui-kit'
import { formatRelative, nextCronTime } from '../../lib/cron'
import { apiSend } from '../../api/client'
import type { SourcePermission } from '../../api/settings'

/* ------------------------------------------------------------------ */
/*   Header (+ ATELIER + · MEMORIA)                                    */
/* ------------------------------------------------------------------ */

interface HeaderProps {
  scheduleCron: string
  lastDuration: string | null
  failedCount: number
  /** Persist a new cron expression (or ``"manual"``) and refresh overview. */
  onScheduleSave: (next: string) => Promise<void>
}

export function Header({
  scheduleCron,
  lastDuration,
  failedCount,
  onScheduleSave,
}: HeaderProps) {
  // Local editor state — kept off the overview snapshot so the user can type
  // freely without each keystroke racing a /api/settings round-trip.
  const [draft, setDraft] = useState(scheduleCron)
  const [dirty, setDirty] = useState(false)
  // Reflect external changes (initial load, after-save refresh) into the
  // draft when the user isn't mid-edit.
  useEffect(() => {
    if (!dirty) setDraft(scheduleCron)
  }, [scheduleCron, dirty])
  const enabled = scheduleCron !== 'manual'

  const commit = async (next: string) => {
    await onScheduleSave(next)
    setDirty(false)
  }
  return (
    <div style={{ marginBottom: 12 }}>
      <div
        style={{
          display: 'flex',
          alignItems: 'center',
          gap: 8,
          flexWrap: 'wrap',
          rowGap: 6,
        }}
      >
        <EstormiLogoMark letter="M" size={42} seed={77} />
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
            flex: 1,
            minWidth: 0,
          }}
        >
          {"emoria"}
        </h2>
      </div>

      {/* Schedule row — always visible: toggle + cron input + "next in X"
          hint. The previous folded chip + duplicate editor were collapsed
          into this single line per user request. Saving persists via
          /api/settings PUT; the backend applies the new schedule at the
          next server reboot (see lifespan.py — scheduler is wired at
          startup). */}
      <div
        style={{
          display: 'flex',
          flexWrap: 'wrap',
          alignItems: 'center',
          gap: 8,
          marginTop: 6,
          fontFamily: 'var(--font-mono)',
          fontSize: 11,
          color: 'var(--ink-dim)',
        }}
      >
        <Switch
          checked={enabled}
          onChange={(on) => {
            if (on) {
              const next = draft && draft !== 'manual' ? draft : '0 3 * * *'
              setDraft(next)
              void commit(next)
            } else {
              void commit('manual')
            }
          }}
          label="⏱"
          ariaLabel="Enable scheduled runs"
          title="Enable scheduled runs"
          dimWhenOff
        />
        <TextInput
          type="text"
          value={draft === 'manual' ? '' : draft}
          placeholder="0 3 * * *"
          disabled={!enabled}
          onChange={(e) => {
            setDraft(e.target.value)
            setDirty(true)
          }}
          onKeyDown={(e) => {
            if (e.key === 'Enter') void commit(draft.trim() || 'manual')
            if (e.key === 'Escape') {
              setDraft(scheduleCron)
              setDirty(false)
            }
          }}
          spellCheck={false}
          aria-label="Cron schedule"
          style={{ flex: '0 1 130px', minWidth: 110 }}
        />
        {dirty ? (
          <GhostAction
            label="Save"
            size="sm"
            onClick={() => void commit(draft.trim() || 'manual')}
          />
        ) : (
          <span title={enabled ? 'Next scheduled run' : 'Scheduled runs disabled'}>
            {enabled
              ? `· next in ${formatRelative(nextCronTime(scheduleCron))}`
              : '· manual only'}
          </span>
        )}
        {lastDuration && <span>· last ran in {lastDuration}</span>}
        {failedCount > 0 && (
          <span style={{ color: 'var(--rouge-clair)' }}>· {failedCount} failed</span>
        )}
      </div>
    </div>
  )
}

/* ------------------------------------------------------------------ */
/*   Table header                                                      */
/* ------------------------------------------------------------------ */

export function TableHeader() {
  const headStyle: React.CSSProperties = {
    fontFamily: 'var(--font-display)',
    fontSize: 11,
    letterSpacing: '0.28em',
    color: 'var(--or-ancien)',
    textTransform: 'uppercase',
    fontWeight: 600,
  }
  // Header mirrors SourceRow's 6-column grid:
  //   [toggle] [name+status] [watermark] [chunks] [run/stop] [kebab]
  // Only SOURCE, WATERMARK and CHUNKS carry visible labels — the toggle,
  // run/stop and kebab columns have their own affordances and stay blank.
  return (
    <div
      role="row"
      style={{
        display: 'grid',
        // Must mirror SourceRow's grid so the column labels sit above the
        // cells they describe — same 26 / 1fr / 115 / 52 / 22 / 22 widths
        // and 10-px gap.
        gridTemplateColumns: '26px 1fr 115px 52px 22px 22px',
        gap: 10,
        padding: '6px 10px',
        borderBottom: '1px solid var(--gilt-line-strong)',
      }}
    >
      <span aria-hidden />
      <span style={{ ...headStyle, fontSize: 9 }}>{"Source"}</span>
      <span style={{ ...headStyle, fontSize: 9, textAlign: 'right' }}>
        {"Watermark"}
      </span>
      <span style={{ ...headStyle, fontSize: 9, textAlign: 'right' }}>
        {"Chunks"}
      </span>
      <span aria-hidden />
      <span aria-hidden />
    </div>
  )
}

/* ------------------------------------------------------------------ */
/*   Permission notice                                                 */
/* ------------------------------------------------------------------ */

/**
 * Surfaced after activating a source whose macOS permission was not
 * cleanly granted. `denied` is a hard stop the user must fix; `manual`
 * (Full Disk Access) and `undetermined` are amber "needs attention".
 * Carries a button straight to the relevant System Settings pane.
 */
export function PermissionNotice({
  source,
  perm,
  onDismiss,
}: {
  source: string
  perm: SourcePermission
  onDismiss: () => void
}) {
  const accent = perm.status === 'denied' ? 'var(--rouge-clair)' : 'var(--or-ancien)'
  // Hoist to a const so the narrowing survives into the onClick closure.
  const pane = perm.settings_pane
  return (
    <div
      role="alert"
      style={{
        margin: '12px 0 6px',
        padding: '10px 12px',
        background: 'var(--encre)',
        border: `1px solid ${accent}`,
        borderLeft: `3px solid ${accent}`,
        borderRadius: 'var(--radius-tight)',
        fontFamily: 'var(--font-ui)',
        fontSize: 14,
        color: 'var(--parchemin)',
        lineHeight: 1.5,
      }}
    >
      <strong style={{ color: accent }}>{source}.</strong> {perm.detail}
      <div style={{ marginTop: 10, display: 'flex', gap: 8 }}>
        {pane && (
          <GhostAction
            label="Open System Settings"
            size="sm"
            onClick={() => {
              void apiSend('/api/open-url', 'POST', { url: pane }).catch(() => {
                /* best-effort: nothing actionable if `open` fails */
              })
            }}
          />
        )}
        <GhostAction label="Dismiss" size="sm" onClick={onDismiss} />
      </div>
    </div>
  )
}
