/**
 * ConnectionPanel — the generic "ingests automatically" connection block, plus
 * the iMessage Full-Disk-Access recovery panel it falls back to. Extracted from
 * SourceManageModal.tsx.
 */
import { useCallback, useEffect, useRef, useState } from 'react'
import { GhostAction } from '@estormi/ui-kit'
import { Eyebrow } from './shared'
import { apiSend } from '../../api/client'
import { recheckFda } from '../../api/settings'
import type { SourceConnection, SourceRowDescriptor } from '../SourceRow'

// The Full Disk Access pane deep-link, shared by both iMessage panels below.
// Mirrored in the open-url allow-list in estormi_server/api/system.py.
const FDA_PANE_URL = 'x-apple.systempreferences:com.apple.preference.security?Privacy_AllFiles'

function openFdaSettings() {
  return apiSend('/api/open-url', 'POST', { url: FDA_PANE_URL }).catch(() => {
    /* best-effort: nothing actionable if `open` fails */
  })
}

function IMessageFdaPanel({ onChanged }: { onChanged?: () => void }) {
  const [phase, setPhase] = useState<'missing' | 'checking' | 'relaunch'>('missing')
  // Whether the user has opened System Settings yet. We only auto-re-check on
  // window focus *after* they've gone to grant it, so an incidental focus
  // (alt-tab away and back) doesn't spend the rate-limited probe.
  const openedRef = useRef(false)
  // Throttle focus-driven re-checks so rapid focus churn can't exceed the
  // server's recheck-fda rate limit.
  const lastCheckRef = useRef(0)

  const recheck = useCallback(async () => {
    setPhase('checking')
    lastCheckRef.current = Date.now()
    try {
      const res = await recheckFda()
      if (res?.status === 'authorized') {
        // Grant detected live — let the page catch up; this panel unmounts.
        onChanged?.()
        return
      }
    } catch {
      /* best-effort: fall through to the relaunch fallback */
    }
    setPhase('relaunch')
  }, [onChanged])

  useEffect(() => {
    const onFocus = () => {
      if (!openedRef.current) return
      if (Date.now() - lastCheckRef.current < 3000) return
      void recheck()
    }
    window.addEventListener('focus', onFocus)
    return () => window.removeEventListener('focus', onFocus)
  }, [recheck])

  const openSettings = () => {
    openedRef.current = true
    void openFdaSettings()
  }

  return (
    <>
      <Eyebrow text={"Connection"} />
      <div
        style={{
          padding: '12px 14px',
          background: 'var(--encre)',
          border: '1px solid var(--rouge-clair)',
          borderLeft: '3px solid var(--rouge-clair)',
          fontFamily: 'var(--font-ui)',
          fontSize: 14,
          color: 'var(--parchemin)',
          lineHeight: 1.5,
        }}
      >
        <strong style={{ color: 'var(--rouge-clair)' }}>Full Disk Access required.</strong>{' '}
        Estormi reads your Messages history locally to weave it into your
        briefing. macOS gates that behind Full Disk Access and offers no
        automatic prompt — you grant it once, by hand.
        <ol style={{ margin: '10px 0 0', paddingLeft: 18 }}>
          <li>Open the Full Disk Access pane below.</li>
          <li>Switch <strong>Estormi</strong> on (or drag it into the list).</li>
          <li>Come back here — we&rsquo;ll re-check automatically.</li>
        </ol>
        {phase === 'relaunch' && (
          <div style={{ marginTop: 10, color: 'var(--or-clair)' }}>
            Still not detected. If you just granted it, quit Estormi
            completely and reopen it — macOS only applies the change to a
            fresh launch.
          </div>
        )}
        <div style={{ marginTop: 10, display: 'flex', gap: 10, alignItems: 'center' }}>
          <GhostAction
            label={"Open Full Disk Access settings"}
            size="sm"
            onClick={openSettings}
          />
          <GhostAction
            label={phase === 'checking' ? 'Checking…' : 'Re-check now'}
            size="sm"
            onClick={() => void recheck()}
          />
        </div>
      </div>
    </>
  )
}

/**
 * IMessageFdaInfo — the calm, informational counterpart to IMessageFdaPanel.
 * Shown whenever iMessage is *not* in a confirmed-failed state (FDA granted, or
 * not yet probed): iMessage still *requires* Full Disk Access — macOS keeps
 * chat.db behind it and offers no automatic prompt — so we always explain how
 * to grant it rather than the misleading "no extra configuration required".
 * When the probe later reports FDA missing, ConnectionPanel swaps in the red
 * recovery panel with its live re-check loop instead.
 */
function IMessageFdaInfo() {
  return (
    <>
      <Eyebrow text={"Connection"} />
      <div
        style={{
          padding: '12px 14px',
          background: 'var(--encre)',
          border: '1px solid var(--gilt-line)',
          borderLeft: '3px solid var(--or-clair)',
          fontFamily: 'var(--font-ui)',
          fontSize: 14,
          color: 'var(--parchemin)',
          lineHeight: 1.5,
        }}
      >
        <strong style={{ color: 'var(--or-clair)' }}>Full Disk Access required.</strong>{' '}
        iMessage reads your Messages history locally to weave it into your
        briefing. macOS gates that behind Full Disk Access and offers no
        automatic prompt — you grant it once, by hand.
        <ol style={{ margin: '10px 0 0', paddingLeft: 18 }}>
          <li>Open the Full Disk Access pane below.</li>
          <li>Switch <strong>Estormi</strong> on (or drag it into the list).</li>
          <li>Quit Estormi completely and reopen it — macOS only applies the
            change to a fresh launch.</li>
        </ol>
        <div style={{ marginTop: 10 }}>
          <GhostAction
            label={"Open Full Disk Access settings"}
            size="sm"
            onClick={() => void openFdaSettings()}
          />
        </div>
      </div>
    </>
  )
}

/**
 * IMessageFdaGranted — the confirmed-grant state: the Tauri shell probed
 * chat.db and Full Disk Access is present (`imessage_fda === true`), so iMessage
 * really does ingest automatically. We say so honestly (sage-green, the
 * "Connected" colour) and still expose the Settings deep-link in case the user
 * wants to review or revoke it.
 */
function IMessageFdaGranted() {
  return (
    <>
      <Eyebrow text={"Connection"} />
      <div
        style={{
          padding: '12px 14px',
          background: 'var(--encre)',
          border: '1px solid var(--gilt-line)',
          borderLeft: '3px solid var(--vert-sauge-sombre)',
          fontFamily: 'var(--font-ui)',
          fontSize: 14,
          color: 'var(--parchemin)',
          lineHeight: 1.5,
        }}
      >
        <strong style={{ color: 'var(--vert-sauge)' }}>✓ Full Disk Access granted.</strong>{' '}
        iMessage can read your Messages history — it ingests automatically on
        each run, no further setup needed.
        <div style={{ marginTop: 10 }}>
          <GhostAction
            label={"Open Full Disk Access settings"}
            size="sm"
            onClick={() => void openFdaSettings()}
          />
        </div>
      </div>
    </>
  )
}

export function ConnectionPanel({
  desc,
  connection,
  fdaGranted,
  onChanged,
}: {
  desc: SourceRowDescriptor
  connection: SourceConnection
  /** iMessage only: true when the Tauri shell confirmed Full Disk Access
   *  (`imessage_fda === true`). Undefined / false everywhere else. */
  fdaGranted?: boolean
  onChanged?: () => void
}) {
  // iMessage always needs Full Disk Access — never claim "no configuration
  // required". Failed → red recovery panel (with live re-check); confirmed
  // grant → a green "all set" panel; otherwise (not yet probed) → the
  // informational how-to-grant panel.
  if (desc.key === 'imessage') {
    if (connection === 'failed') return <IMessageFdaPanel onChanged={onChanged} />
    if (fdaGranted) return <IMessageFdaGranted />
    return <IMessageFdaInfo />
  }

  return (
    <>
      <Eyebrow text={"Connection"} />
      <div
        style={{
          padding: '12px 14px',
          background: 'var(--encre)',
          border: '1px solid var(--gilt-line)',
          fontFamily: 'var(--font-ui)',
          fontSize: 14,
          color: 'var(--ink-dim)',
          lineHeight: 1.5,
        }}
      >
        {/* TODO(backend): expose a per-source connection probe so we can
            render account / status / latency here. For now this source
            ingests automatically with no extra configuration. */}
        {desc.label} ingests automatically — no extra configuration required.
      </div>
    </>
  )
}

/* ------------------------------------------------------------------ */
/*   Small UI helpers                                                  */
/* ------------------------------------------------------------------ */
