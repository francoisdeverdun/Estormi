/**
 * ManageDetails — the right-column body of SourceManageModal: the historic-depth
 * picker, the sync-state / watermark panel, and the danger zone (reset data,
 * clear log, disconnect) with its inline confirmation. Extracted from
 * SourceManageModal.tsx; all the stateful handlers stay in the parent and are
 * passed in as props so this component is purely presentational.
 */
import { GhostAction } from '@estormi/ui-kit'
import type { SourceRowDescriptor } from '../SourceRow'
import { WATERMARK_MECHANISM } from '../SourceRow'
import { DangerButton, Eyebrow } from '../sourcepanels/shared'
import {
  DEPTH_SOURCES,
  depthOptions,
  historicHint,
  type HistoricOption,
} from './depth'

export type DangerKind = 'reset-data' | 'reset-log' | 'disconnect'

function canDisconnect(key: string): boolean {
  return key === 'whatsapp' || key === 'gcal' || key === 'whoop'
}

export function ManageDetails({
  desc,
  watermark,
  busy,
  historic,
  confirmDanger,
  error,
  onSaveHistoric,
  onResetSyncToken,
  onResetWatermark,
  onConfirmDanger,
  setConfirmDanger,
}: {
  desc: SourceRowDescriptor
  watermark: string
  busy: boolean
  historic: HistoricOption
  confirmDanger: DangerKind | null
  error: string | null
  onSaveHistoric: (value: HistoricOption) => void
  onResetSyncToken: () => void
  onResetWatermark: () => void
  onConfirmDanger: () => void
  setConfirmDanger: (kind: DangerKind | null) => void
}) {
  return (
    <div
      style={{
        padding: '16px 18px',
        background: 'rgba(0,0,0,0.15)',
      }}
    >
      {DEPTH_SOURCES.has(desc.key) && (
        <>
          <Eyebrow
            text={desc.key === 'knowledge' ? 'First ingestion window' : 'Historic depth'}
          />
          <div
            style={{
              padding: '10px 12px',
              background: 'var(--encre)',
              border: '1px solid var(--gilt-line)',
              marginBottom: 16,
            }}
          >
            <div
              style={{
                display: 'grid',
                gridTemplateColumns: 'repeat(5, 1fr)',
                gap: 4,
                marginBottom: 8,
              }}
              role="radiogroup"
              aria-label={
                desc.key === 'knowledge' ? 'First ingestion window' : 'Historic depth'
              }
            >
              {depthOptions(desc.key).map((o) => {
                const selected = historic === o
                return (
                  <button
                    key={o}
                    type="button"
                    role="radio"
                    aria-checked={selected}
                    onClick={() => onSaveHistoric(o)}
                    style={{
                      padding: '6px 4px',
                      background: selected
                        ? 'color-mix(in srgb, var(--pourpre) 20%, transparent)'
                        : 'transparent',
                      border: `1px solid ${
                        selected ? 'var(--pourpre-clair)' : 'var(--gilt-line)'
                      }`,
                      color: selected ? 'var(--parchemin)' : 'var(--ink-dim)',
                      fontFamily: 'var(--font-display)',
                      fontSize: 12,
                      letterSpacing: '0.14em',
                      textTransform: 'uppercase',
                      fontWeight: selected ? 700 : 500,
                      cursor: 'pointer',
                    }}
                  >
                    {o}
                  </button>
                )
              })}
            </div>
            <div
              style={{
                fontFamily: 'var(--font-ui)',
                fontSize: 13,
                color: 'var(--ink-dim)',
                lineHeight: 1.45,
              }}
            >
              {historicHint(desc.key, historic)}
            </div>
          </div>
        </>
      )}

      {/* Watermark panel only for sources that actually use the
          ingestion_watermarks table. gcal/whatsapp track progress
          another way (see WATERMARK_MECHANISM) — a reset button
          there would do nothing. */}
      {WATERMARK_MECHANISM[desc.key] ? (
        <>
          <Eyebrow text={"Sync state"} />
          <div
            style={{
              padding: '10px 12px',
              background: 'var(--encre)',
              border: '1px solid var(--gilt-line)',
              marginBottom: 18,
            }}
          >
            <div
              style={{
                fontFamily: 'var(--font-ui)',
                fontSize: 13,
                color: 'var(--ink-dim)',
                lineHeight: 1.5,
              }}
            >
              {desc.label} does not use a watermark — it syncs
              incrementally via {WATERMARK_MECHANISM[desc.key]}.{' '}
              {desc.key === 'gcal'
                ? 'Reset the sync token to force a full re-pull on the next run, or use Reset data below to also drop existing events.'
                : 'To re-pull everything, use Reset data below.'}
            </div>
            {desc.key === 'gcal' && (
              <div style={{ marginTop: 10 }}>
                <GhostAction
                  label={"Reset sync token"}
                  size="sm"
                  disabled={busy}
                  onClick={onResetSyncToken}
                />
              </div>
            )}
          </div>
        </>
      ) : (
        <>
          <Eyebrow text={"Watermark"} />
          <div
            style={{
              padding: '10px 12px',
              background: 'var(--encre)',
              border: '1px solid var(--gilt-line)',
              marginBottom: 18,
            }}
          >
            <div
              style={{
                fontFamily: 'var(--font-mono)',
                fontSize: 13,
                color: 'var(--parchemin)',
              }}
            >
              {watermark || '—'}
            </div>
            <div
              style={{
                fontFamily: 'var(--font-ui)',
                fontSize: 12,
                color: 'var(--ink-dim)',
                marginTop: 4,
              }}
            >
              Next pickup will resume from this point.
            </div>
            <div style={{ display: 'flex', gap: 6, marginTop: 10 }}>
              <GhostAction
                label={"Reset watermark"}
                size="sm"
                disabled={busy}
                onClick={onResetWatermark}
              />
            </div>
          </div>
        </>
      )}

      <Eyebrow text={"Danger zone"} />
      <div
        style={{
          padding: '12px 14px',
          background: 'color-mix(in srgb, var(--rouge-clair) 8%, transparent)',
          border: '1px solid color-mix(in srgb, var(--rouge-clair) 40%, transparent)',
          borderLeft: '3px solid var(--rouge-clair)',
        }}
      >
        <div
          style={{
            fontFamily: 'var(--font-ui)',
            fontSize: 14,
            color: 'var(--parchemin)',
            marginBottom: 10,
            lineHeight: 1.5,
          }}
        >
          {desc.key === 'whatsapp' ? (
            <>
              Reset data drops the derived chunks + embeddings and the
              watermark; the durable message log is kept, so the next run
              re-derives them with no rescan. Clear message log also wipes
              the raw captured messages — the next run must re-scan WhatsApp.
            </>
          ) : (
            <>
              Reset data drops all chunks and embeddings for this source.
              Watermark is reset to zero. The next run will re-ingest
              everything.
            </>
          )}
        </div>
        <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap' }}>
          <DangerButton
            label={"Reset data"}
            disabled={busy}
            onClick={() => setConfirmDanger('reset-data')}
          />
          {desc.key === 'whatsapp' && (
            <DangerButton
              label={"Clear message log"}
              disabled={busy}
              onClick={() => setConfirmDanger('reset-log')}
            />
          )}
          <DangerButton
            label={"Disconnect"}
            disabled={busy || !canDisconnect(desc.key)}
            onClick={() => setConfirmDanger('disconnect')}
          />
        </div>
        {confirmDanger && (
          <div
            role="alertdialog"
            aria-label={"Confirm destructive action"}
            style={{
              marginTop: 12,
              padding: 10,
              border: '1px solid var(--rouge-clair)',
              background: 'var(--overlay-pourpre)',
              fontFamily: 'var(--font-ui)',
              fontSize: 14,
              color: 'var(--parchemin)',
            }}
          >
            <div style={{ marginBottom: 8 }}>
              {confirmDanger === 'reset-data'
                ? desc.key === 'whatsapp'
                  ? `This deletes WhatsApp's chunks, vectors and watermark only — the durable message log is kept, so the next run re-derives them with no rescan. Continue?`
                  : `This deletes only ${desc.label}'s chunks, vectors and watermark — other sources are untouched. The next ingest re-pulls it from scratch. Cannot be undone. Continue?`
                : confirmDanger === 'reset-log'
                  ? `This wipes WhatsApp's raw message log AND its chunks. The captured messages cannot be recovered from the log — the next run must re-scan WhatsApp (and can only re-fetch what its servers still hold). Cannot be undone. Continue?`
                  : `Disconnect ${desc.label}? You'll need to re-pair / re-authenticate.`}
            </div>
            <div style={{ display: 'flex', gap: 6 }}>
              <GhostAction
                label={"Cancel"}
                size="sm"
                onClick={() => setConfirmDanger(null)}
              />
              <DangerButton
                label={"Confirm"}
                disabled={busy}
                onClick={onConfirmDanger}
              />
            </div>
          </div>
        )}
      </div>

      {error && (
        <div
          role="alert"
          style={{
            marginTop: 12,
            padding: '8px 10px',
            border: '1px solid var(--rouge-clair)',
            color: 'var(--rouge-clair)',
            fontFamily: 'var(--font-mono)',
            fontSize: 13,
          }}
        >
          {error}
        </div>
      )}
    </div>
  )
}
