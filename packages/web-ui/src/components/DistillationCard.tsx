/**
 * DistillationCard — "distill my quill" in one gesture, plus its schedule.
 *
 * Trains the local prose quill (QLoRA on-device) on the briefings already in
 * your vault — composed locally and corrected by hand, edited on macOS or by
 * editing the iCloud-Drive file directly — then evaluates against held-out days
 * and installs the fused model as the local-only SFT tier. Nothing leaves the
 * Mac and nothing is published to your briefing history. The more you correct
 * your briefings, the better the quill gets — so it runs on a schedule
 * (default Sunday 03:00) and self-improves.
 *
 * The card is a thin view over GET /api/distill/status; the engine owns all
 * state. While a chain runs, the phase line mirrors the engine room.
 */
import { useEffect, useRef, useState } from 'react'
import { GhostAction, PrimaryAction, Switch, TextInput } from '@estormi/ui-kit'
import { Mono } from './Mono'
import {
  deleteDistillTooling,
  distillToolingInstallPath,
  getDistillStatus,
  runDistill,
  type DistillStatus,
} from '../api/distill'
import { getSettings, updateSettings } from '../api/settings'
import { ModelDownloadList, type DownloadListItem } from './ModelDownloadList'

const DEFAULT_CRON = '0 3 * * 0' // Sunday 03:00, local time
const PHASE_LABELS: Record<string, string> = {
  setup: 'Installing the training toolchain (first run)',
  harvest: 'Reading your briefing archive',
  dataset: 'Building the dataset',
  train: 'Training (QLoRA)',
  eval: 'Evaluating on held-out days',
  fuse: 'Fusing → GGUF',
  yielded: 'Yielded — waiting for the engine slot',
  done: 'Done',
  rejected: 'Rejected',
  failed: 'Failed',
}

export function DistillationCard() {
  const [status, setStatus] = useState<DistillStatus | null>(null)
  const [busy, setBusy] = useState(false)
  const [notice, setNotice] = useState('')
  const [unavailable, setUnavailable] = useState(false)
  // Schedule (cron, or "manual"). Local draft kept off the settings snapshot so
  // typing doesn't race an /api/settings round-trip per keystroke.
  const [cron, setCron] = useState(DEFAULT_CRON)
  const [cronDraft, setCronDraft] = useState(DEFAULT_CRON)
  const [cronDirty, setCronDirty] = useState(false)
  const timer = useRef<number | null>(null)

  const refresh = async () => {
    try {
      const next = await getDistillStatus()
      if (next) {
        setStatus(next)
        setUnavailable(false)
      } else {
        setUnavailable(true)
      }
    } catch {
      // Older server without the endpoint, or transient — keep polling.
      setUnavailable(true)
    }
  }

  useEffect(() => {
    void refresh()
    void getSettings()
      .then((s) => {
        const value = s.distill_schedule_cron || DEFAULT_CRON
        setCron(value)
        if (!cronDirty) setCronDraft(value)
      })
      .catch(() => undefined)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  // Poll faster while a chain is running so the phase line tracks the engine.
  const phase = status?.status.phase ?? ''
  // "active" must track a real running engine — NOT the last phase string in
  // status.json. A killed/crashed run leaves its in-flight phase (e.g. "train")
  // behind with no terminal write, so trusting the phase would strand the card
  // on "Training…" with the button disabled forever. The queue snapshot is the
  // source of truth for what is actually executing.
  const active = (status?.running ?? []).some((e) => e.kind === 'distill')
  useEffect(() => {
    if (timer.current) window.clearInterval(timer.current)
    timer.current = window.setInterval(() => void refresh(), active ? 5_000 : 30_000)
    return () => {
      if (timer.current) window.clearInterval(timer.current)
    }
  }, [active])

  const cronEnabled = cron !== 'manual'
  const commitCron = async (next: string) => {
    setCron(next)
    setCronDirty(false)
    try {
      await updateSettings({ distill_schedule_cron: next })
    } catch (e) {
      setNotice(e instanceof Error ? e.message : 'Could not save the schedule.')
    }
  }
  const onRun = async () => {
    setBusy(true)
    setNotice('')
    try {
      const res = await runDistill()
      const outcome = res?.status ?? 'unavailable'
      setNotice(outcome === 'queued' ? 'Queued — see the engine room.' : `Engine ${outcome}.`)
      void refresh()
    } catch (e) {
      setNotice(e instanceof Error ? e.message : 'Could not start the distillation.')
    } finally {
      setBusy(false)
    }
  }

  if (!status) {
    return (
      <Mono dim>
        {unavailable
          ? 'Distillation unavailable on this server (update the app).'
          : 'Loading distillation status…'}
      </Mono>
    )
  }

  const { references, tooling, installed } = status
  const edited = references.models['user-edited'] ?? 0
  const vaultCount = references.vaultCount ?? 0
  // Distillation needs a floor of briefings before the dataset is worth
  // training on; the server is the source of truth for the threshold.
  const minBriefings = references.minBriefings ?? 5
  const trainable = Math.max(references.count, vaultCount)
  const enoughBriefings = trainable >= minBriefings
  const refsLine =
    references.count > 0
      ? `${references.count} briefing day(s) · ${edited} hand-edited`
      : vaultCount > 0
        ? `${vaultCount} briefing day(s) ready to train on`
        : 'no briefings to train on yet'
  const verdict = status.status.verdict
  // The MLX toolchain installs like a model — its own Download/Delete row below —
  // and the distill button stays disabled until it's present. (A scheduled run
  // still self-bootstraps the toolchain via engine phase ⓪.)
  const toolItem: DownloadListItem = {
    key: 'tooling',
    label: 'Training toolchain (MLX)',
    subtitle: '~1 GB · Apple Silicon',
    downloaded: tooling.ready,
  }

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
      <Mono>
        {installed ? '✦ distilled quill installed' : 'no distilled quill yet'} · {refsLine}
      </Mono>

      {/* Schedule — on/off + manual cron entry, like the briefing schedule. */}
      <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
        <Switch
          checked={cronEnabled}
          onChange={(on) => {
            if (on) {
              const next = cronDraft && cronDraft !== 'manual' ? cronDraft : DEFAULT_CRON
              setCronDraft(next)
              void commitCron(next)
            } else {
              void commitCron('manual')
            }
          }}
          label="⏱ schedule"
          ariaLabel="Retrain the quill automatically on this schedule"
          title="Retrain the quill automatically on this schedule"
          dimWhenOff
        />
        <TextInput
          type="text"
          value={cronDraft === 'manual' ? '' : cronDraft}
          placeholder={DEFAULT_CRON}
          disabled={!cronEnabled}
          onChange={(e) => {
            setCronDraft(e.target.value)
            setCronDirty(true)
          }}
          onKeyDown={(e) => {
            if (e.key === 'Enter') void commitCron(cronDraft.trim() || 'manual')
            if (e.key === 'Escape') {
              setCronDraft(cron)
              setCronDirty(false)
            }
          }}
          spellCheck={false}
          aria-label="Distillation cron schedule"
          style={{ flex: '0 1 130px', minWidth: 110 }}
        />
        {cronDirty ? (
          <GhostAction
            label="Save"
            size="sm"
            onClick={() => void commitCron(cronDraft.trim() || 'manual')}
          />
        ) : (
          <Mono dim>{cronEnabled ? '· weekly by default (Sun 03:00)' : '· manual only'}</Mono>
        )}
      </div>

      {/* The training toolchain installs like a model — Download/Delete + progress. */}
      <ModelDownloadList
        items={[toolItem]}
        downloadPath={distillToolingInstallPath}
        onDelete={() => deleteDistillTooling()}
        deleteConfirm={() => 'Remove the MLX training toolchain (~1 GB)? You can reinstall it anytime.'}
        onChanged={refresh}
      />

      <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
        <span
          title={
            !enoughBriefings
              ? `Distillation needs at least ${minBriefings} briefings to train on — you have ${trainable}.`
              : tooling.ready
                ? 'Trains the local quill on all the briefings in your vault (on-device QLoRA, a few minutes to an hour). Nothing leaves your Mac and nothing is published to your briefing history.'
                : 'Download the training toolchain above first.'
          }
        >
          <PrimaryAction
            label={installed ? 'Re-distill my quill' : 'Distill my quill'}
            size="sm"
            onClick={() => void onRun()}
            disabled={busy || active || !tooling.ready || !enoughBriefings}
          />
        </span>
        {active && <Mono>{PHASE_LABELS[phase] ?? phase}…</Mono>}
      </div>

      {!enoughBriefings && !active && (
        <Mono dim>
          Needs at least {minBriefings} briefings to train on — {trainable} so far. Compose a few
          more days, then distill your quill.
        </Mono>
      )}
      {enoughBriefings && !tooling.ready && !active && (
        <Mono dim>Download the toolchain above to enable distillation.</Mono>
      )}
      {phase === 'done' && status.status.lastTrainedAt && (
        <Mono dim>last trained {status.status.lastTrainedAt.slice(0, 16).replace('T', ' ')}Z</Mono>
      )}
      {verdict && (
        <Mono dim>
          eval{verdict.artifact === 'gguf' ? ' (installed model)' : ''}: {verdict.tunedClean}/
          {verdict.prompts} clean outputs (base {verdict.baseClean}/{verdict.prompts}) →{' '}
          {verdict.pass ? 'installed' : 'kept the previous quill'}
        </Mono>
      )}
      {(phase === 'failed' || phase === 'rejected') && status.status.error && (
        <Mono dim>{status.status.error}</Mono>
      )}
      {notice && <Mono dim>{notice}</Mono>}
    </div>
  )
}
