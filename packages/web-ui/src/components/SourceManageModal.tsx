/**
 * SourceManageModal — full-bleed per-source "Manage" pane.
 *
 * Built directly (not reusing the small confirm `Modal`) because the
 * design's body is two-column and roughly 920px wide. Visual language
 * still matches the rest of the gilt-and-charbon system:
 *
 *   - IlluminatedRule at the top of the dialog
 *   - or-ancien hairline top accent
 *   - 1px gilt-line-strong frame on charbon
 *
 * Layout:
 *   ┌─────────────────────────────────────────────────────────────┐
 *   │ IlluminatedRule                                              │
 *   │ HEADER  icon + eyebrow + name + status pill + summary  ×    │
 *   ├──────────────────────────┬──────────────────────────────────┤
 *   │ LEFT  pairing / chats /  │ RIGHT  historic depth · watermark│
 *   │       calendars / paths  │        · danger zone             │
 *   ├──────────────────────────┴──────────────────────────────────┤
 *   │ FOOTER  "Changes apply on next run"           Cancel · SAVE  │
 *   └─────────────────────────────────────────────────────────────┘
 *
 * Endpoint wiring (only what the backend already exposes):
 *
 *   GET    /api/whatsapp/chats           list paired chats
 *   PATCH  /api/whatsapp/chats/{id}      tag a chat
 *   POST   /api/whatsapp/reset           clear pairing                (Disconnect for WhatsApp)
 *   DELETE /api/calendar/auth            disconnect Google OAuth      (Disconnect for gcal)
 *   POST   /api/google-calendar/sync-token/reset     drop gcal sync tokens
 *   POST   /api/pick-folder              native folder picker          (Pick folder)
 *   PUT    /api/sources/{name}/watermark/reset       reset watermark
 *   PUT    /api/settings                 persist historic-depth value
 *
 * `<source>_historic_depth` is applied on the next run via
 * `apply_ingest_env_overrides` in estormi_server/server/launchers/ingestion.py.
 */
import { useCallback, useEffect, useState } from 'react'
import { IlluminatedRule, PrimaryAction } from '@estormi/ui-kit'
import { ModalOverlay } from './ModalOverlay'
import type { SourceRowDescriptor, SourceConnection } from './SourceRow'
import {
  resetWatermark,
  updateSettings,
  resetSourceData,
  resetWhatsAppLog,
} from '../api/settings'
import {
  disconnectCalendarOAuth,
  resetGoogleCalendarSyncToken,
  resetWhatsAppPairing,
  disconnectWhoop,
} from '../api/sources_ext'

import { ManageHeader } from './sourcemanage/ManageHeader'
import { LeftPanel } from './sourcemanage/LeftPanel'
import { ManageDetails, type DangerKind } from './sourcemanage/ManageDetails'
import { normaliseHistoric, type HistoricOption } from './sourcemanage/depth'

export interface SourceManageModalProps {
  open: boolean
  desc: SourceRowDescriptor | null
  connection: SourceConnection
  /** iMessage only: Full Disk Access confirmed granted by the Tauri shell. */
  fdaGranted?: boolean
  chunks: number
  watermark: string
  historicDepth?: string
  onClose: () => void
  /** Called after a backend write that may change overview state. */
  onChanged?: () => void
}

export function SourceManageModal({
  open,
  desc,
  connection,
  fdaGranted,
  chunks,
  watermark,
  historicDepth,
  onClose,
  onChanged,
}: SourceManageModalProps) {
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [historic, setHistoric] = useState<HistoricOption>(
    normaliseHistoric(desc?.key, historicDepth),
  )
  const [confirmDanger, setConfirmDanger] = useState<DangerKind | null>(null)

  useEffect(() => {
    setHistoric(normaliseHistoric(desc?.key, historicDepth))
    setError(null)
    setConfirmDanger(null)
  }, [desc?.key, historicDepth])

  const safeAction = useCallback(
    async (fn: () => Promise<unknown>) => {
      setBusy(true)
      try {
        await fn()
        setError(null)
        onChanged?.()
      } catch (e) {
        setError(e instanceof Error ? e.message : String(e))
      } finally {
        setBusy(false)
      }
    },
    [onChanged],
  )

  const onResetWatermark = () =>
    desc && safeAction(() => resetWatermark(desc.dbKey))

  const onResetSyncToken = () =>
    desc && safeAction(() => resetGoogleCalendarSyncToken())

  const onSaveHistoric = (value: HistoricOption) => {
    setHistoric(value)
    if (!desc) return
    void safeAction(() =>
      updateSettings({ [`${desc.key}_historic_depth`]: value.toLowerCase() }),
    )
  }

  const onConfirmDanger = async () => {
    if (!desc || !confirmDanger) return
    if (confirmDanger === 'reset-data') {
      // Per-source reset: drops ONLY this source's chunks, vectors and
      // watermark. For WhatsApp the durable message log is kept, so the chunks
      // re-derive on the next run with no rescan.
      await safeAction(() => resetSourceData(desc.dbKey))
    } else if (confirmDanger === 'reset-log') {
      // WhatsApp-only: wipe the raw durable message log too — needs a rescan.
      await safeAction(() => resetWhatsAppLog())
    } else if (confirmDanger === 'disconnect') {
      if (desc.key === 'whatsapp') {
        await safeAction(() => resetWhatsAppPairing())
      } else if (desc.key === 'gcal') {
        await safeAction(() => disconnectCalendarOAuth())
      } else if (desc.key === 'whoop') {
        await safeAction(() => disconnectWhoop())
      } else {
        setError('Disconnect not supported for this source yet.')
      }
    }
    setConfirmDanger(null)
  }

  if (!open || !desc) return null

  return (
    <ModalOverlay
      onClose={onClose}
      closeOnScrim="drag-guard"
      escape="plain"
      lockBodyScroll
      scrimBackground="var(--scrim-modal)"
      scrimBackdropFilter="blur(6px)"
      labelledBy="source-manage-title"
      dialogStyle={{
        width: '100%',
        maxWidth: 720,
        maxHeight: 'calc(100vh - 48px)',
        background: 'var(--charbon)',
        border: '1px solid var(--gilt-line-strong)',
        borderTop: '3px solid var(--or-ancien)',
        borderRadius: 'var(--radius-panel)',
        boxShadow: '0 20px 80px var(--shadow-modal)',
        display: 'flex',
        flexDirection: 'column',
        overflow: 'hidden',
        position: 'relative',
      }}
    >
      <div style={{ padding: '10px 28px 0' }}>
        <IlluminatedRule />
      </div>

      {/* HEADER */}
      <ManageHeader
        desc={desc}
        connection={connection}
        chunks={chunks}
        watermark={watermark}
      />

      {/* BODY — single column stack: a 2-col layout (connection | depth)
          overflows the 520-px shell and clips the "Reset watermark" /
          "Danger zone" controls off the right edge. */}
      <div
        style={{
          display: 'flex',
          flexDirection: 'column',
          flex: 1,
          minHeight: 0,
          overflowY: 'auto',
        }}
      >
        <div
          style={{
            padding: '16px 18px',
            borderBottom: '1px solid var(--gilt-line)',
          }}
        >
          <LeftPanel
            desc={desc}
            connection={connection}
            fdaGranted={fdaGranted}
            onChanged={onChanged}
          />
        </div>

        <ManageDetails
          desc={desc}
          watermark={watermark}
          busy={busy}
          historic={historic}
          confirmDanger={confirmDanger}
          error={error}
          onSaveHistoric={onSaveHistoric}
          onResetSyncToken={() => void onResetSyncToken()}
          onResetWatermark={() => void onResetWatermark()}
          onConfirmDanger={() => void onConfirmDanger()}
          setConfirmDanger={setConfirmDanger}
        />
      </div>

      {/* FOOTER */}
      <div
        style={{
          padding: '12px 24px',
          borderTop: '1px solid var(--gilt-line)',
          background: 'var(--well-dim)',
          display: 'flex',
          justifyContent: 'space-between',
          alignItems: 'center',
          gap: 12,
        }}
      >
        <span
          style={{
            fontFamily: 'var(--font-display)',
            fontStyle: 'italic',
            fontSize: 11,
            letterSpacing: '0.22em',
            color: 'var(--ink-dim)',
            textTransform: 'uppercase',
          }}
        >
          Changes saved automatically · apply on next run
        </span>
        <div style={{ display: 'flex', gap: 8 }}>
          <PrimaryAction label={"Close"} size="sm" onClick={onClose} />
        </div>
      </div>
    </ModalOverlay>
  )
}
