/**
 * StageProcession — the engine-room run visualisation, extracted from
 * EngineRoomPopover.tsx.
 *
 * Renders the ingestion stage strip and the per-source "procession" of nodes
 * from pipeline state, terminating at the vault. Pure presentation driven by
 * the pipeline/settings hooks; the popover composes it alongside the queue and
 * engine grid. The station sub-components (ProcessionNode, VaultTerminus,
 * StageStripBar) and their shared geometry/status helpers live in
 * `procession/`; LiveDot lives in `./LiveDot`.
 */
import { useEffect, useState } from 'react'
import { Diamond } from '@estormi/ui-kit'
import { FormattedLog } from '../log/LogStream'
import { isEnabled } from '../SourcesPanel'
import type { PipelineStage } from '../../api/pipeline'
import { usePipelineSnapshot } from '../../hooks/usePipeline'
import { useSettings } from '../../hooks/useSettings'
import { DAG_MARKER, DAG_RUN_START, SOURCE_MARKER, SOURCE_RUN_START } from '../../lib/logFormat'
import { ProcessionNode } from './procession/ProcessionNode'
import { StageStripBar } from './procession/StageStripBar'
import { VaultTerminus } from './procession/VaultTerminus'
import { TERMINAL_STATUSES, fmtClock, stageLabel } from './procession/shared'

/**
 * IngestionStageBody — the redesigned ingestion log view: a clickable strip
 * of stage squares up top (one per DAG stage, coloured by live status) and
 * the selected stage's own log below. Selection auto-follows the running
 * stage until the user pins one by clicking; a "raw log" toggle falls back to
 * the flat aggregate DAG log for debugging.
 */
export function IngestionStageBody({ color }: { color: string }) {
  // Read the shared pipeline snapshot — SourcesPanel (always mounted) owns the
  // single /api/pipeline poll, so this modal piggybacks instead of opening a
  // second identical poller while it's on screen.
  const data = usePipelineSnapshot()
  const { settings } = useSettings()
  const allStages: PipelineStage[] = data?.stages ?? []
  // Show every *enabled* source — including those still ``pending`` (not yet
  // reached this run, or never reached because the run stopped early): they
  // belong to the run's plan and the strip should reflect it. A disabled or
  // unconfigured source logs no real DAG stage, so the backend renders it
  // ``pending`` too; those inert tiles are noise, so drop a ``pending`` stage
  // whose source is disabled. A stage with any non-pending status (running,
  // ok, fail, cancelled) took part in the run and is always kept.
  const stages: PipelineStage[] = allStages.filter(
    (s) => s.status !== 'pending' || (settings ? isEnabled(settings, s.name) : false),
  )
  const [pinned, setPinned] = useState<string | null>(null)
  const [raw, setRaw] = useState(false)
  const [now, setNow] = useState(() => Date.now())

  const runningName = stages.find((s) => s.status === 'running')?.name ?? null
  const lastTerminal =
    [...stages].reverse().find((s) => TERMINAL_STATUSES.has(s.status))?.name ?? null
  const autoName = runningName ?? lastTerminal ?? stages[0]?.name ?? null
  const selected = pinned && stages.some((s) => s.name === pinned) ? pinned : autoName
  const selectedStage = stages.find((s) => s.name === selected) ?? null
  const selectedRunning = selectedStage?.status === 'running'

  // 1 s ticker while a stage is running — drives the live mm:ss on the running
  // chip between the 5 s pipeline polls.
  useEffect(() => {
    if (!runningName) return
    const id = window.setInterval(() => setNow(Date.now()), 1000)
    return () => window.clearInterval(id)
  }, [runningName])

  if (raw) {
    return (
      <>
        <StageStripBar color={color} raw onToggleRaw={() => setRaw(false)} label="raw DAG log" />
        {/* Engine-room view → only the run in progress (or the latest one):
            scope to the last "starting daily ingestion DAG" marker. */}
        <FormattedLog
          url="/api/pipeline/stage-log?engine=ingestion"
          pollMs={runningName ? 4000 : 0}
          parseOpts={{ runStartRe: DAG_RUN_START, markerRe: DAG_MARKER }}
          autoScroll={!!runningName}
        />
      </>
    )
  }

  const chunksBySource = data?.last_run_chunks_by_source ?? {}
  // The gilt rail fills left→right up to the furthest-progressed stage (the
  // running one, or — when idle — the last terminal one). Everything past it
  // stays a dim hairline, so the eye reads how far the procession advanced.
  const progressIndex = stages.reduce(
    (acc, s, i) => (s.status === 'running' || TERMINAL_STATUSES.has(s.status) ? i : acc),
    -1,
  )
  const doneCount = stages.filter((s) => TERMINAL_STATUSES.has(s.status)).length
  const runComplete = !runningName && progressIndex >= stages.length - 1 && stages.length > 0
  const totalChunks = data?.last_run_chunks_added ?? 0

  return (
    <>
      <div
        style={{
          flex: '0 0 auto',
          padding: '9px 12px 8px',
          borderBottom: '1px solid var(--gilt-line)',
        }}
      >
        {stages.length === 0 ? (
          <div style={{ fontSize: 11, color: 'var(--ink-dim)', fontStyle: 'italic', padding: '4px 2px' }}>
            No run recorded yet.
          </div>
        ) : (
          <>
            <div
              style={{
                display: 'flex',
                alignItems: 'baseline',
                justifyContent: 'space-between',
                marginBottom: 4,
                fontFamily: 'var(--font-display)',
                fontSize: 9,
                letterSpacing: '0.22em',
                textTransform: 'uppercase',
                color: 'var(--or-ancien)',
              }}
            >
              <span style={{ display: 'inline-flex', alignItems: 'center', gap: 5 }}>
                <Diamond size={5} color="var(--or-ancien)" /> The Procession
              </span>
              <span
                style={{
                  fontFamily: 'var(--font-mono)',
                  letterSpacing: 0,
                  color: 'var(--ink-dim)',
                }}
              >
                {doneCount}/{stages.length}
                {totalChunks > 0 ? ` · +${totalChunks.toLocaleString()}` : ''}
              </span>
            </div>
            {/* Not role="list": the stages are interactive <button>s, so a
                list/listitem wrapper would clobber their button semantics. */}
            <div style={{ display: 'flex', alignItems: 'flex-start', overflowX: 'auto', paddingTop: 2 }}>
              {stages.map((s, i) => (
                <ProcessionNode
                  key={s.name}
                  stage={s}
                  isFirst={i === 0}
                  leftFilled={i <= progressIndex}
                  rightFilled={i < progressIndex}
                  chunks={chunksBySource[s.name] ?? 0}
                  selected={s.name === selected}
                  now={now}
                  onSelect={() => setPinned(s.name)}
                />
              ))}
              <VaultTerminus reached={runComplete} chunks={totalChunks} />
            </div>
          </>
        )}
      </div>
      <StageStripBar
        color={color}
        raw={false}
        onToggleRaw={() => setRaw(true)}
        label={selected ? stageLabel(selected) : '—'}
        sub={
          selectedStage
            ? selectedRunning && selectedStage.started_at_epoch_ms
              ? `running · ${fmtClock(Math.max(0, Math.floor((now - selectedStage.started_at_epoch_ms) / 1000)))}`
              : selectedStage.status === 'pending'
                ? 'pending'
                : `${selectedStage.status}${selectedStage.duration && selectedStage.duration !== '—' ? ` · ${selectedStage.duration}` : ''}`
            : undefined
        }
      />
      {selected ? (
        // Engine-room view → only the current run of the selected stage:
        // scope to the last "── run …" boundary in its per-source log.
        <FormattedLog
          key={selected}
          url={`/api/pipeline/stage-log?source=${encodeURIComponent(selected)}&lines=400`}
          pollMs={selectedRunning ? 3000 : 0}
          parseOpts={{ runStartRe: SOURCE_RUN_START, markerRe: SOURCE_MARKER }}
          autoScroll={selectedRunning}
        />
      ) : (
        <div style={{ flex: 1, padding: '12px 16px', fontSize: 12, color: 'var(--ink-dim)', fontStyle: 'italic' }}>
          Select a stage to view its log.
        </div>
      )}
    </>
  )
}
