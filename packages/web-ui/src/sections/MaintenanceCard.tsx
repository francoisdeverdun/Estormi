/**
 * MaintenanceCard — the one-pager's consolidated maintenance surface.
 *
 * Sub-sections:
 *   1. Storage — DB / Qdrant / staging size graph (StorageBar)
 *   2. Briefing — how the daily briefing is *composed*: schedule, reasoning
 *      backend (BriefingBuildControls). Source ingestion is configured
 *      separately on the External knowledge source.
 *   3. Models — the LLM catalog: install state + per-model download.
 *   4. Distillation — the local-quill retrain schedule + run (DistillationCard).
 *   5. Voice — the TTS catalog (briefing narration): install state + download.
 *
 * The briefing reset lives on the briefing modal (Briefing → resetBriefings),
 * not here.
 */
import { useEffect, useState } from 'react'
import { GildedPanel, SectionHeader } from '@estormi/ui-kit'
import { StorageBar } from '../components/StorageBar'
import { StorageLocationCard } from '../components/StorageLocationCard'
import { ModelDownloadList, type DownloadListItem } from '../components/ModelDownloadList'
import { type Overview } from '../api/overview'
import { useOverviewPoll } from '../state/useOverviewPoll'
import { getModelCatalog, deleteModel, type ModelCatalog } from '../api/model'
import { getTtsCatalog, deleteTtsModel, type TtsCatalog } from '../api/tts'
import { getStorageLocation, type StorageLocation } from '../api/storage'
import { useSnapshotState } from '../state/snapshotCache'
import { BriefingBuildControls } from '../components/briefing/BriefingBuildControls'
import { DistillationCard } from '../components/DistillationCard'
import { Hint } from '../components/Mono'
import { fmtBytes } from '../lib/format'

export function MaintenanceCard() {
  const [overview] = useSnapshotState<Overview | null>('overview', null)
  const [catalog, setCatalog] = useState<ModelCatalog | null>(null)
  const [ttsCatalog, setTtsCatalog] = useState<TtsCatalog | null>(null)
  const [storageLoc, setStorageLoc] = useState<StorageLocation | null>(null)

  const storage = overview?.storage ?? null

  const refreshCatalog = async () => {
    try {
      setCatalog(await getModelCatalog())
    } catch {
      /* silent; polled again shortly */
    }
  }
  const refreshTts = async () => {
    try {
      setTtsCatalog(await getTtsCatalog())
    } catch {
      /* silent; polled again shortly */
    }
  }
  const refreshStorageLoc = async () => {
    try {
      setStorageLoc(await getStorageLocation())
    } catch {
      /* silent; polled again shortly */
    }
  }

  // The overview snapshot is driven by the shared poller; here we only poll the
  // model + voice catalogs (download/install state) and the storage location.
  useOverviewPoll()
  useEffect(() => {
    void refreshCatalog()
    void refreshTts()
    void refreshStorageLoc()
    const id = window.setInterval(() => {
      void refreshCatalog()
      void refreshTts()
      void refreshStorageLoc()
    }, 15_000)
    return () => window.clearInterval(id)
  }, [])

  // The WhatsApp durable log lives *inside* estormi.db, so break it out of the
  // SQLite figure rather than adding it on top — keeps the bar total equal to
  // the real on-disk size.
  const waCache = storage?.whatsapp_cache_bytes ?? 0
  const storageSegments = storage
    ? [
        {
          label: 'SQLite',
          bytes: Math.max(0, storage.db_bytes - waCache),
          color: 'var(--or-ancien)',
        },
        { label: 'Qdrant', bytes: storage.qdrant_bytes, color: 'var(--enluminure-clair)' },
        { label: 'WhatsApp cache', bytes: waCache, color: 'var(--vert-sauge)' },
        { label: 'Staging', bytes: storage.staging_bytes, color: 'var(--pourpre-clair)' },
      ]
    : []

  // Whole-library footprint + volume free space, shown up in the StorageBar
  // header (the StorageLocationCard below no longer repeats it). Free turns red
  // when it can't hold the current library.
  const lowFree =
    storageLoc?.freeGb != null && storageLoc.freeGb < storageLoc.libraryBytes / 1024 ** 3
  const storageDetail = storageLoc ? (
    <>
      {fmtBytes(storageLoc.libraryBytes)} on disk
      {storageLoc.freeGb != null && (
        <>
          {' · '}
          <span style={{ color: lowFree ? 'var(--rouge-clair)' : 'var(--ink-dim)' }}>
            {storageLoc.freeGb} GB free
          </span>
        </>
      )}
    </>
  ) : undefined

  // The briefing runs both local quills (two-quills routing) plus the narration
  // voice, so they install as ONE turn-key resource — "Briefing models" — rather
  // than three separate rows. The two base GGUFs and the Voxtral voice are
  // bundled; the locally-distilled quill (local_only, produced on-device) still
  // shows as its own deletable row once it exists.
  const BUNDLE_KEY = 'briefing-bundle'
  const baseTiers = ['ministral3-14b', 'gemma4-12b']
  const baseModels = catalog?.models.filter((m) => baseTiers.includes(m.tier)) ?? []
  const distilledModels = catalog?.models.filter((m) => !baseTiers.includes(m.tier)) ?? []
  const voxtral = ttsCatalog?.models?.[0] ?? null

  const bundleParts = [...baseModels, ...(voxtral ? [voxtral] : [])]
  const bundleDownloaded =
    bundleParts.length > 0 && bundleParts.every((m) => m.downloaded)
  const bundleBytes = bundleParts.reduce(
    (sum, m) => sum + (m.downloaded ? m.size_bytes : m.expected_bytes),
    0,
  )

  // Both catalogs must have loaded before the bundle row can be shown (it spans
  // the model + voice catalogs), so render the loading state until both arrive.
  const modelItems: DownloadListItem[] | null =
    catalog && ttsCatalog
      ? [
          {
            key: BUNDLE_KEY,
            label: 'Briefing models',
            subtitle: `${fmtBytes(bundleBytes)} · 2 quills + voice · ≥ 16 GB`,
            downloaded: bundleDownloaded,
          },
          ...distilledModels.map((m) => ({
            key: m.tier,
            label: m.label,
            subtitle: `${fmtBytes(m.size_bytes)} · ≥ ${m.min_ram_gb} GB`,
            downloaded: m.downloaded,
          })),
        ]
      : null

  const bundleDownloadPath = (key: string) =>
    key === BUNDLE_KEY
      ? '/api/model/bundle/download'
      : `/api/model/download?tier=${encodeURIComponent(key)}`

  const deleteModelItem = async (key: string) => {
    if (key !== BUNDLE_KEY) return deleteModel(key)
    // Bundle delete: drop both quills and the voice in turn (best-effort —
    // a missing component is a no-op on the server).
    for (const m of baseModels) await deleteModel(m.tier).catch(() => undefined)
    if (voxtral) await deleteTtsModel(voxtral.key).catch(() => undefined)
  }

  const refreshModelsAndVoice = () => {
    void refreshCatalog()
    void refreshTts()
  }

  return (
    <GildedPanel>
      <SectionHeader title={'Officina'} letter="O" />

      {/* STORAGE — size breakdown + whole-library footprint/free up here, with
          the single root storage location (current path + picker) below. */}
      <SubHeader text={'Storage'} />
      {storage ? (
        <StorageBar segments={storageSegments} detail={storageDetail} />
      ) : (
        <Hint>—</Hint>
      )}
      <StorageLocationCard loc={storageLoc} onRelocated={() => void refreshStorageLoc()} />

      {/* BRIEFING BUILD — how the daily briefing is composed:
          schedule, output language, reasoning backend. Source
          ingestion is configured separately on the External
          knowledge source. */}
      <SubHeader text={'Briefing'} />
      <div style={{ marginBottom: 12 }}>
        <BriefingBuildControls />
      </div>

      {/* BRIEFING MODELS — both local quills + the narration voice as one
          turn-key download. The distilled quill (when present) lists below it. */}
      <SubHeader text={'Models'} muted />
      <div style={{ display: 'flex', flexDirection: 'column', gap: 4, marginBottom: 16 }}>
        <ModelDownloadList
          items={modelItems}
          downloadPath={bundleDownloadPath}
          onDelete={deleteModelItem}
          deleteConfirm={(label) =>
            label === 'Briefing models'
              ? 'Delete the briefing models (both quills + voice)? The files will be removed from disk.'
              : `Delete ${label}? The GGUF file will be removed from disk.`
          }
          onChanged={refreshModelsAndVoice}
          loadingText="Loading models…"
        />
      </div>

      {/* DISTILLATION — retrain the local prose quill (QLoRA, on-device) on your
          own vault briefings. One gesture, plus a schedule; nothing leaves the
          Mac and the fused adapter installs as the local-only prose tier. */}
      <SubHeader text={'Distillation'} muted />
      <div style={{ display: 'flex', flexDirection: 'column', gap: 4, marginBottom: 16 }}>
        <DistillationCard />
      </div>

      {/* Resets moved out — each engine's reset lives on its own modal. */}
    </GildedPanel>
  )
}

/* ─────────────────── Helpers ─────────────────── */

function SubHeader({ text, muted = false }: { text: string; muted?: boolean }) {
  return (
    <div
      style={{
        fontFamily: 'var(--font-display)',
        fontSize: 9,
        letterSpacing: '0.28em',
        color: muted ? 'var(--ink-dim)' : 'var(--or-ancien)',
        textTransform: 'uppercase',
        marginTop: 14,
        marginBottom: 8,
      }}
    >
      {text}
    </div>
  )
}
