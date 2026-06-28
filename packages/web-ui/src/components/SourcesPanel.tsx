/**
 * SourcesPanel — the rich "+ ATELIER + · Memoria" card.
 *
 *   1. Header eyebrow "+ ATELIER +" (Cinzel gold, fleurons),
 *      illuminated cap "M" + "EMORIA" gradient title,
 *      Inter subtitle.
 *   2. 5-column table with header eyebrow row
 *      (SOURCE · CONNECTION · CHUNKS · WATERMARK · ACTIONS).
 *   3. One `SourceRow` per source (toggle + brand icon + name/path +
 *      connection pill + chunks + watermark + ⋮ Manage opener).
 *   4. Per-source `SourceManageModal` opened by the ⋮ control; watermark
 *      and data resets live in its Watermark + Danger-zone sections.
 *
 * Reads from `/api/settings/overview`:
 *
 *   - sources.counts[<dbKey>]      → current chunk count
 *   - sources.watermarks[<dbKey>]  → watermark text or empty
 *   - settings.source_<key>_enabled → on/off
 *   - settings.<key>_historic_depth → default historic-depth selection
 *
 * Writes:
 *
 *   - POST /api/sources/<key>/toggle        toggle on/off
 *
 * Framed in a plain `GildedPanel gold` — vine borders are reserved for the
 * page hero (one per board); supporting cards stay sober with the standard
 * gilt-line frame.
 */
import { useState } from 'react'
import { GildedPanel } from '@estormi/ui-kit'
import {
  SourceRow,
  type SourceRowDescriptor,
  type SourceConnection,
} from './SourceRow'
import { SourceManageModal } from './SourceManageModal'
import { SourceHistoryModal } from './SourceHistoryModal'
import type { Overview } from '../api/overview'
import { toggleSource, updateSettings, type SourcePermission } from '../api/settings'
import { Header, TableHeader, PermissionNotice } from './sourcestable/parts'
import { useSourcePipeline } from './sourcestable/useSourcePipeline'
import {
  isAwaitingSetup,
  liveStageView,
  sourceHistory,
} from './sourcestable/dag'

/**
 * Canonical source list, in display order. The pairing flag tells the
 * manage modal whether to render a pairing/connection block.
 */
export const SOURCES: ReadonlyArray<SourceRowDescriptor> = [
  { key: 'notes', label: 'Apple Notes', dbKey: 'notes' },
  { key: 'mail', label: 'Apple Mail', dbKey: 'mail' },
  { key: 'gcal', label: 'Google Calendar', dbKey: 'gcal', needsPairing: true },
  { key: 'whoop', label: 'WHOOP', dbKey: 'whoop', needsPairing: true },
  { key: 'reminders', label: 'Reminders', dbKey: 'reminders' },
  { key: 'imessage', label: 'iMessage', dbKey: 'imessage' },
  { key: 'whatsapp', label: 'WhatsApp', dbKey: 'whatsapp', needsPairing: true },
  { key: 'documents', label: 'Documents', dbKey: 'documents' },
  // External knowledge (YouTube transcripts + RSS) ingested as world-corpus
  // memory. Stage key `knowledge`; chunks land under source `knowledge`, so
  // the count + watermark read from dbKey `knowledge`. Its Manage modal hosts
  // the briefing-sources panel (feeds + schedule/language/LLM).
  { key: 'knowledge', label: 'External knowledge', dbKey: 'knowledge' },
]

export interface SourcesPanelProps {
  overview: Overview | null
  refreshOverview: () => Promise<void>
}

export function SourcesPanel({ overview, refreshOverview }: SourcesPanelProps) {
  const [error, setError] = useState<string | null>(null)
  const [manageSrc, setManageSrc] = useState<SourceRowDescriptor | null>(null)
  const [permNotice, setPermNotice] = useState<
    { source: string; perm: SourcePermission } | null
  >(null)

  /* DAG plumbing — the merged panel runs the source pipeline directly. */
  const {
    pipeline,
    pipelineError,
    optimisticStage,
    stageActionError,
    stageByName,
    runOnly,
    stopAll,
  } = useSourcePipeline()
  // Click on the source name → full history modal (live status + run list
  // + per-run log).
  const [openHistorySource, setOpenHistorySource] = useState<{
    stage: string
    label: string
  } | null>(null)

  const history = pipeline?.history ?? []
  const failedCount = pipeline?.last_run_failed_stages?.length ?? 0
  const scheduleCron = (overview?.settings ?? {})['schedule_cron'] || '0 3 * * *'

  const onToggle = async (desc: SourceRowDescriptor, next: boolean) => {
    try {
      const res = await toggleSource(desc.key, next)
      setError(null)
      // Activating a source triggers + verifies its macOS permission on
      // the backend. Surface anything that still needs the user's
      // attention; a clean grant — or a source with no macOS permission,
      // or disabling — clears the notice. ``res`` is null when the
      // endpoint returned an empty body (shouldn't happen for this route,
      // but the type system forces us to handle it).
      const perm = next ? (res?.permission ?? null) : null
      if (perm && perm.status !== 'authorized' && perm.status !== 'unavailable') {
        setPermNotice({ source: desc.label, perm })
      } else {
        setPermNotice(null)
      }
      await refreshOverview()
      // When the user activates a source that still needs setup, drop them
      // straight into Manage so the QR scan / OAuth dance or folder pick is
      // right there — they shouldn't have to hunt for it.
      if (next && needsSetupOnActivate(desc, overview)) setManageSrc(desc)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    }
  }

  // The ⋮ button now opens Manage directly — Reset watermark + Reset
  // data live inside Manage (Watermark + Danger zone sections), so a
  // popover with duplicates was pure indirection.
  const onKebab = (desc: SourceRowDescriptor) => () => {
    setManageSrc(desc)
  }

  const settings = overview?.settings ?? {}
  const counts = overview?.sources?.counts ?? {}
  const watermarks = overview?.sources?.watermarks ?? {}

  return (
    <GildedPanel gold>
      <div style={{ padding: '4px 6px 6px' }}>
        <Header
          scheduleCron={scheduleCron}
          lastDuration={pipeline?.last_run_duration ?? null}
          failedCount={failedCount}
          onScheduleSave={async (next) => {
            try {
              await updateSettings({ schedule_cron: next })
              setError(null)
              await refreshOverview()
            } catch (e) {
              setError(e instanceof Error ? e.message : String(e))
            }
          }}
        />
      </div>

      <div style={{ padding: '0 16px' }}>
        {/* `role="table"` gives the rowgroup/row descendants a valid ancestor —
            without it the SourceRow / TableHeader rows are orphaned ARIA roles. */}
        <div role="table" aria-label="Sources">
          <div role="rowgroup">
            <TableHeader />
          </div>
          <div role="rowgroup">
            {SOURCES.map((d) => {
            const enabled = isEnabled(settings, d.key)
            const conn = connectionFor(d, overview, enabled)
            const stage = stageByName.get(d.key.toLowerCase())
            const { status: runStatus, duration } = liveStageView(
              stage,
              optimisticStage === d.key,
            )
            const stageHist = sourceHistory(d.key, history, !!pipeline?.is_running)
            return (
              <SourceRow
                key={d.key}
                desc={d}
                enabled={enabled}
                connection={conn}
                chunks={counts[d.dbKey] ?? 0}
                watermark={watermarks[d.dbKey] ?? ''}
                onToggle={() => void onToggle(d, !enabled)}
                onAction={onKebab(d)}
                runStatus={runStatus}
                duration={duration}
                history={stageHist}
                awaitingSetup={isAwaitingSetup(d.key, settings)}
                // Any engine run holds the run-scoped lock, so a single-source
                // run would just 409. Surface that by greying every play button
                // while a run is in flight (the running stage shows ■ instead).
                runInProgress={!!pipeline?.is_running}
                onRunOnly={() => void runOnly(d.key)}
                onStopAll={() => void stopAll()}
                onOpenHistory={() =>
                  setOpenHistorySource({ stage: d.key, label: d.label })
                }
              />
            )
          })}
          </div>
        </div>
        {stageActionError && (
          <div
            role="alert"
            style={{
              margin: '8px 0',
              padding: '6px 10px',
              border: '1px solid var(--rouge-clair)',
              color: 'var(--rouge-clair)',
              fontFamily: 'var(--font-mono)',
              fontSize: 11,
            }}
          >
            {stageActionError}
          </div>
        )}
        {/* The 5 s pipeline poll failed (typically the sidecar is unreachable);
            the stage rows below are stale. A faint note rather than a loud
            alert — the global backend-down banner carries the headline. */}
        {pipelineError && (
          <div
            style={{
              margin: '6px 0',
              color: 'var(--ink-dim)',
              fontFamily: 'var(--font-mono)',
              fontSize: 11,
            }}
          >
            Pipeline status unavailable — retrying…
          </div>
        )}
      </div>

      <div style={{ padding: '4px 22px 0' }}>
        {error && (
          <div
            role="alert"
            style={{
              margin: '12px 0 6px',
              padding: '8px 12px',
              border: '1px solid var(--rouge-clair)',
              color: 'var(--rouge-clair)',
              fontFamily: 'var(--font-mono)',
              fontSize: 13,
            }}
          >
            {error}
          </div>
        )}
        {permNotice && (
          <PermissionNotice
            source={permNotice.source}
            perm={permNotice.perm}
            onDismiss={() => setPermNotice(null)}
          />
        )}
      </div>

      {/* Per-source Manage modal */}
      <SourceManageModal
        open={manageSrc !== null}
        desc={manageSrc}
        connection={
          manageSrc
            ? connectionFor(manageSrc, overview, isEnabled(settings, manageSrc.key))
            : 'unknown'
        }
        fdaGranted={overview?.permissions?.imessage_fda === true}
        chunks={manageSrc ? (counts[manageSrc.dbKey] ?? 0) : 0}
        watermark={manageSrc ? (watermarks[manageSrc.dbKey] ?? '') : ''}
        historicDepth={
          manageSrc ? settings[`${manageSrc.key}_historic_depth`] : undefined
        }
        onClose={() => setManageSrc(null)}
        onChanged={() => void refreshOverview()}
      />

      {openHistorySource && (() => {
        const stageInfo = stageByName.get(openHistorySource.stage.toLowerCase())
        const { status: liveStatus, duration: liveDuration } = liveStageView(
          stageInfo,
          optimisticStage === openHistorySource.stage,
        )
        return (
          <SourceHistoryModal
            stage={openHistorySource.stage}
            label={openHistorySource.label}
            liveStatus={liveStatus}
            liveDuration={liveDuration}
            onClose={() => setOpenHistorySource(null)}
          />
        )
      })()}
    </GildedPanel>
  )
}

/* ------------------------------------------------------------------ */
/*   Helpers                                                           */
/* ------------------------------------------------------------------ */

export function isEnabled(settings: Record<string, string>, key: string): boolean {
  const v = settings[`source_${key}_enabled`]
  // Match the backend default in estormi_server/server/jobs.py: sources are
  // off until the user explicitly flips the toggle.
  if (v === undefined || v === null || v === '') return false
  return v !== 'false' && v !== '0' && v !== 'off'
}

/**
 * Whether activating `desc` should drop the user straight into Manage.
 *
 * - Pairing sources (gcal, whatsapp, whoop) always route in — the QR
 *   scan / OAuth dance lives there.
 * - The folder-rooted source (documents) routes in only while its
 *   `documents_root` is unset; re-enabling an already-configured folder
 *   source is a plain toggle. Mirrors the root check in `connectionFor`.
 */
function needsSetupOnActivate(
  desc: SourceRowDescriptor,
  overview: Overview | null,
): boolean {
  if (desc.needsPairing) return true
  if (desc.key === 'documents') {
    const root = (overview?.settings ?? {})[`${desc.key}_root`]
    return !root || !String(root).trim()
  }
  return false
}

function connectionFor(
  desc: SourceRowDescriptor,
  overview: Overview | null,
  enabled: boolean,
): SourceConnection {
  // WhatsApp is pairing-driven, not toggle-driven: an unpaired account needs a
  // QR scan, which is far more actionable than a bare "disabled". So its
  // scan/connected state is decided FIRST, before the generic enable-toggle
  // check below — an unpaired WhatsApp shows "Awaiting scan" even if the toggle
  // is off (the toggle still gates ingestion; this is only the display chip).
  // `paired` is the sticky "user has scanned the QR" bit (false → re-scan
  // needed); falling back to `connected` keeps older sidecars (no `paired`
  // field) working. `session_state == "UNPAIRED"` flips `paired` to false in
  // the backend (handle_status), so a dropped link surfaces here.
  if (desc.key === 'whatsapp') {
    if (!overview) return 'unknown'
    const wa = overview.whatsapp
    const paired = wa?.paired ?? wa?.connected ?? false
    if (!paired) return 'awaiting-scan'
    if (!enabled) return 'disabled'
    return 'connected'
  }

  // Check "no overview yet" BEFORE the enable-toggle: with no overview the
  // `enabled` flag is derived from empty settings (always false), so reporting
  // "disabled" would mislabel every source during a cold-start outage. An
  // absent overview is genuinely "unknown", not "off".
  if (!overview) return 'unknown'
  if (!enabled) return 'disabled'

  // "Error" is reserved for a real connection / auth / permission
  // failure — not a transient run failure. A red bar in the DAG
  // timeline + the global "N stages failed" pill on the hero already
  // signal a bad run; we don't double-tar the source row.

  // The folder-rooted source (`documents`) needs an explicit path
  // before it can ingest. Without `documents_root` in settings, the
  // ingester refuses to run, so the chip must reflect "not set up"
  // rather than "Connected" — even when the toggle is on.
  if (desc.key === 'documents') {
    const root = (overview.settings ?? {})[`${desc.key}_root`]
    if (!root || !String(root).trim()) return 'awaiting-scan'
  }

  // iMessage needs macOS Full Disk Access. The Tauri shell probes chat.db
  // at launch (the sandboxed Python sidecar can't stat it). macOS has no
  // API to *request* FDA, so when the probe says it's missing we surface
  // an honest Error instead of a false "Connected" — the next run would
  // otherwise just fail silently. `null` (not yet probed) stays optimistic.
  if (desc.key === 'imessage' && overview.permissions?.imessage_fda === false) {
    return 'failed'
  }

  // Other sources don't have a connection probe yet — an enabled toggle
  // implies the user granted access (TCC / OAuth / picked a folder).
  return 'connected'
}
