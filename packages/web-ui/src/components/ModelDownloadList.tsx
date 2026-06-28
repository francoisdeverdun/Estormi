/**
 * ModelDownloadList — a catalog of downloadable models with per-item
 * download (streamed over EventSource) and delete.
 *
 * Extracted from MaintenanceCard so the LLM model catalog and the TTS (voice)
 * catalog share one implementation and one look. Each row shows a label + a
 * size/requirement subtitle and a Download or Delete action; an in-flight
 * download renders a progress bar fed by the SSE endpoint. The list is generic:
 * the caller supplies the items (pre-formatted), the SSE download URL per key,
 * and the delete call.
 */
import { useEffect, useRef, useState } from 'react'
import { GhostAction } from '@estormi/ui-kit'
import { Modal } from './Modal'
import { Hint } from './Mono'

export interface DownloadListItem {
  /** Stable id passed to ``downloadPath`` / ``onDelete``. */
  key: string
  label: string
  /** Right-aligned mono subtitle, e.g. "7.7 GB · ≥ 16 GB". */
  subtitle: string
  downloaded: boolean
}

interface Props {
  /** Catalog rows, or ``null`` while the catalog is still loading. */
  items: DownloadListItem[] | null
  /** SSE (EventSource) URL that streams download progress for ``key``. */
  downloadPath: (key: string) => string
  /** Delete the downloaded artifact for ``key``. */
  onDelete: (key: string) => Promise<unknown>
  /** Confirm-dialog message for a delete. */
  deleteConfirm: (label: string) => string
  /** Called after a download completes or a delete succeeds, to refresh state. */
  onChanged: () => void
  loadingText?: string
}

export function ModelDownloadList({
  items,
  downloadPath,
  onDelete,
  deleteConfirm,
  onChanged,
  loadingText = 'Loading…',
}: Props) {
  const [dlKey, setDlKey] = useState<string | null>(null)
  const [dlProgress, setDlProgress] = useState<number | null>(null)
  const [dlMessage, setDlMessage] = useState<string | null>(null)
  // A failed download clears ``dlKey`` (so ``isDownloading`` goes false), which
  // would otherwise hide the in-flight ``dlMessage`` before the user ever sees
  // why it failed. Keep the error pinned to its row, independent of the
  // in-flight state, until the next download attempt on that row.
  const [dlError, setDlError] = useState<{ key: string; message: string } | null>(null)
  const [deleting, setDeleting] = useState<string | null>(null)
  // Delete is gated by the app's own confirm Modal — NOT window.confirm, which
  // is a silent no-op inside the Tauri WKWebView (the button would do nothing).
  const [pendingDelete, setPendingDelete] = useState<{ key: string; label: string } | null>(null)
  const esRef = useRef<EventSource | null>(null)

  useEffect(
    () => () => {
      esRef.current?.close()
      esRef.current = null
    },
    [],
  )

  const startDownload = (key: string) => {
    if (dlKey) return
    setDlError(null)
    setDlKey(key)
    setDlProgress(0)
    setDlMessage('Starting download…')
    const es = new EventSource(downloadPath(key))
    esRef.current = es
    es.onmessage = (ev: MessageEvent) => {
      try {
        const d = JSON.parse(ev.data) as { progress?: number; message?: string; status?: string }
        if (typeof d.progress === 'number') setDlProgress(d.progress)
        if (d.message) setDlMessage(d.message)
        if (d.status === 'done') {
          es.close()
          esRef.current = null
          setDlKey(null)
          setDlProgress(100)
          onChanged()
        }
        if (d.status === 'error') {
          es.close()
          esRef.current = null
          setDlKey(null)
          setDlError({ key, message: d.message ?? 'Download failed' })
        }
      } catch {
        setDlMessage(ev.data)
      }
    }
    es.onerror = () => {
      es.close()
      esRef.current = null
      setDlKey(null)
      setDlError((e) => e ?? { key, message: 'Connection closed' })
    }
  }

  const confirmDelete = async () => {
    if (!pendingDelete || deleting) return
    const { key } = pendingDelete
    setPendingDelete(null)
    setDeleting(key)
    try {
      await onDelete(key)
      onChanged()
    } finally {
      setDeleting(null)
    }
  }

  if (!items) return <Hint>{loadingText}</Hint>

  return (
    <>
      {items.map((m) => {
        const isDownloading = dlKey === m.key
        const pct = isDownloading ? (dlProgress ?? 0) : m.downloaded ? 100 : 0
        return (
          <div
            key={m.key}
            style={{
              padding: '7px 10px',
              background: 'var(--well-sunk)',
              border: '1px solid var(--gilt-line)',
              borderLeft: `3px solid ${m.downloaded ? 'var(--vert-sauge-sombre)' : 'var(--gilt-line)'}`,
            }}
          >
            <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
              <span
                style={{
                  fontFamily: 'var(--font-display)',
                  fontSize: 12,
                  fontWeight: 700,
                  color: 'var(--parchemin)',
                  letterSpacing: '0.06em',
                  minWidth: 0,
                  flex: 1,
                  overflow: 'hidden',
                  textOverflow: 'ellipsis',
                  whiteSpace: 'nowrap',
                }}
              >
                {m.label}
              </span>
              <span
                style={{
                  fontFamily: 'var(--font-mono)',
                  fontSize: 10,
                  color: 'var(--ink-dim)',
                }}
              >
                {m.subtitle}
              </span>
              {m.downloaded ? (
                <GhostAction
                  label={deleting === m.key ? 'Deleting…' : 'Delete'}
                  tone="danger"
                  size="sm"
                  onClick={() => setPendingDelete({ key: m.key, label: m.label })}
                  disabled={!!deleting || !!dlKey}
                />
              ) : (
                <GhostAction
                  label={isDownloading ? 'Downloading…' : 'Download'}
                  size="sm"
                  onClick={() => startDownload(m.key)}
                  disabled={!!dlKey || !!deleting}
                />
              )}
            </div>
            {isDownloading && (
              <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginTop: 6 }}>
                <div
                  style={{
                    flex: 1,
                    height: 5,
                    background: 'var(--encre)',
                    border: '1px solid var(--gilt-line)',
                  }}
                >
                  <div
                    style={{
                      width: `${pct}%`,
                      height: '100%',
                      background: 'linear-gradient(to right, var(--or-sombre), var(--or-clair))',
                    }}
                  />
                </div>
                <span
                  style={{
                    fontFamily: 'var(--font-mono)',
                    fontSize: 10,
                    color: 'var(--ink-dim)',
                  }}
                >
                  {pct}%
                </span>
              </div>
            )}
            {isDownloading && dlMessage && (
              <div
                style={{
                  marginTop: 4,
                  fontFamily: 'var(--font-mono)',
                  fontSize: 10,
                  color: 'var(--ink-dim)',
                }}
              >
                {dlMessage}
              </div>
            )}
            {dlError?.key === m.key && (
              <div
                role="alert"
                style={{
                  marginTop: 4,
                  fontFamily: 'var(--font-mono)',
                  fontSize: 10,
                  color: 'var(--rouge-clair)',
                }}
              >
                {dlError.message}
              </div>
            )}
          </div>
        )
      })}
      <Modal
        open={!!pendingDelete}
        title="Delete download?"
        body={pendingDelete ? deleteConfirm(pendingDelete.label) : ''}
        confirmLabel="Delete"
        destructive
        onConfirm={() => void confirmDelete()}
        onCancel={() => setPendingDelete(null)}
      />
    </>
  )
}
