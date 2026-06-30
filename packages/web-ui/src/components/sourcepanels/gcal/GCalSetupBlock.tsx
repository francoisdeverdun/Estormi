/**
 * GCalSetupBlock — the "you don't have credentials yet" state of
 * GoogleCalendarPanel, made interactive: the user can drop the JSON
 * they downloaded from Google Cloud Console onto a target inside the
 * app, or click the target to open a file picker. On success the
 * server stores it securely in the macOS Keychain and we
 * advance to the "disconnected" state automatically. The five-step
 * walkthrough lives in GCalSetupGuide. Extracted from GoogleCalendarPanel.tsx.
 */
import { useCallback, useRef, useState } from 'react'
import { uploadGoogleClientSecrets } from '../../../api/sources_ext'
import { GCalSetupGuide } from './GCalSetupGuide'

export function GCalSetupBlock({
  onUploaded,
  onError,
}: {
  onUploaded: () => void
  onError: (msg: string) => void
}) {
  const [busy, setBusy] = useState(false)
  const [hover, setHover] = useState(false)
  const [showGuide, setShowGuide] = useState(false)
  const fileInputRef = useRef<HTMLInputElement | null>(null)

  const handleFile = useCallback(
    async (file: File | null | undefined) => {
      if (!file) return
      if (busy) return
      setBusy(true)
      try {
        const content = await file.text()
        await uploadGoogleClientSecrets(content)
        onUploaded()
      } catch (e) {
        onError(e instanceof Error ? e.message : String(e))
      } finally {
        setBusy(false)
      }
    },
    [busy, onUploaded, onError],
  )

  return (
    <>
      <div style={{ color: 'var(--parchemin)', fontWeight: 600, marginBottom: 6 }}>
        Setup needed — Google OAuth client missing
      </div>
      <div style={{ marginBottom: 10 }}>
        Estormi needs a Google OAuth <em>Desktop app</em> client to talk to
        Calendar on your behalf. If you already have the JSON, drop it on the
        target below. Otherwise, expand the guide and follow the five steps.
      </div>

      <input
        ref={fileInputRef}
        type="file"
        accept="application/json,.json"
        style={{ display: 'none' }}
        onChange={(e) => {
          void handleFile(e.target.files?.[0])
          if (e.target) e.target.value = ''
        }}
      />

      <button
        type="button"
        onClick={() => fileInputRef.current?.click()}
        onDragEnter={(e) => {
          e.preventDefault()
          setHover(true)
        }}
        onDragOver={(e) => {
          e.preventDefault()
          setHover(true)
        }}
        onDragLeave={() => setHover(false)}
        onDrop={(e) => {
          e.preventDefault()
          setHover(false)
          const f = e.dataTransfer.files?.[0]
          void handleFile(f)
        }}
        disabled={busy}
        style={{
          display: 'flex',
          flexDirection: 'column',
          alignItems: 'center',
          justifyContent: 'center',
          gap: 6,
          width: '100%',
          padding: '24px 16px',
          background: hover ? 'var(--overlay-gilt-warm)' : 'var(--well-sunk)',
          border: `1px dashed ${hover ? 'var(--or-clair)' : 'var(--gilt-line-strong)'}`,
          color: 'var(--parchemin)',
          fontFamily: 'var(--font-ui)',
          fontSize: 16,
          cursor: busy ? 'progress' : 'pointer',
          transition: 'background 120ms ease, border-color 120ms ease',
        }}
        aria-label={"Upload Google OAuth client JSON"}
      >
        <span style={{ fontSize: 24, color: 'var(--or-ancien)' }}>↑</span>
        <span>
          {busy ? 'Saving…' : 'Drop google_client_secrets.json here, or click to pick'}
        </span>
        <span style={{ fontSize: 13, color: 'var(--ink-dim)' }}>
          Stored securely in your macOS Keychain
        </span>
      </button>

      <button
        type="button"
        onClick={() => setShowGuide((s) => !s)}
        aria-expanded={showGuide}
        style={{
          marginTop: 12,
          padding: '6px 10px',
          background: 'transparent',
          border: '1px solid var(--gilt-line)',
          color: 'var(--or-ancien)',
          fontFamily: 'var(--font-display)',
          fontSize: 12,
          letterSpacing: '0.18em',
          textTransform: 'uppercase',
          cursor: 'pointer',
        }}
      >
        {showGuide ? '▾' : '▸'} {showGuide ? 'Hide guide' : 'Show step-by-step guide'}
      </button>

      {showGuide && <GCalSetupGuide />}
    </>
  )
}
