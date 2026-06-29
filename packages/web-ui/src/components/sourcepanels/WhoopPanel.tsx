/**
 * WhoopPanel — the connection block for the WHOOP source, extracted from
 * SourceManageModal.tsx.
 *
 * Unlike Google Calendar (file upload + calendar list), WHOOP is a plain
 * OAuth2 flow with no per-resource picker: the user pastes the app's
 * client_id / client_secret (from developer.whoop.com), then clicks Connect
 * to run the consent flow in their browser. A single /api/whoop/status probe
 * drives the three states.
 */
import { useCallback, useEffect, useRef, useState } from 'react'
import { GhostAction, GoldToggle, PrimaryAction, TextInput } from '@estormi/ui-kit'
import { Eyebrow } from './shared'
import { OAuthConnectionBox } from './OAuthConnectionBox'
import { useOAuthConnection, type OAuthConnState } from './useOAuthConnection'
import { getSettings, updateWhoopPolling } from '../../api/settings'
import { refreshKnowledgeHealth } from '../../api/knowledge'
import {
  disconnectWhoop,
  getWhoopStatus,
  openWhoopAuth,
  uploadWhoopCredentials,
  type WhoopStatus,
} from '../../api/sources_ext'

export function WhoopPanel({ onChanged }: { onChanged?: () => void }) {
  const [redirectUri, setRedirectUri] = useState<string>('')

  // WHOOP has no per-resource picker: a single /api/whoop/status probe resolves
  // all three states (and the redirect URI shown in the setup block).
  const probeFn = useCallback(async (): Promise<OAuthConnState> => {
    const s: WhoopStatus = await getWhoopStatus()
    setRedirectUri(s.redirect_uri)
    if (!s.client) return 'setup'
    return s.connected ? 'connected' : 'disconnected'
  }, [])

  const { state, error, setState, setError, probe } = useOAuthConnection(probeFn)

  const startConnect = async () => {
    try {
      await openWhoopAuth()
      setError(null)
      void probe()
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    }
  }

  const disconnect = async () => {
    try {
      await disconnectWhoop()
      setState('disconnected')
      onChanged?.()
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    }
  }

  return (
    <>
      <OAuthConnectionBox
        service="WHOOP"
        state={state}
        error={error}
        onProbe={() => void probe()}
        onConnect={() => void startConnect()}
        onDisconnect={() => void disconnect()}
        disconnectedBody={
          <>
            Click below to open the WHOOP consent screen in your default
            browser. Once you authorise, Estormi will sync your recovery,
            sleep and strain.
          </>
        }
        connectedBody={
          <>
            Token persisted to the data directory. Disconnect below to revoke
            access locally.
          </>
        }
        setup={
          <WhoopSetupBlock
            redirectUri={redirectUri}
            onSaved={() => {
              setError(null)
              void probe()
            }}
            onError={(msg) => setError(msg)}
          />
        }
        trailing={
          error && state !== 'error' ? (
            <div style={{ marginTop: 10, color: 'var(--rouge-clair)', fontSize: 13 }}>{error}</div>
          ) : null
        }
      />
      {state === 'connected' && <WhoopPollingSection />}
    </>
  )
}

/**
 * WhoopPollingSection — the "wake trigger" controls for the WHOOP source.
 *
 * A morning poller (server-side: jobs._schedule_whoop_poll) fires once WHOOP
 * has scored the night's recovery — i.e. shortly after the user wakes. When
 * the morning briefing already exists it refreshes ONLY the readiness card
 * (~1 minute, audio re-narrated in the background); the full ingestion +
 * briefing pipeline runs only when no briefing exists yet. The fixed cron
 * stays as a safety net. The knobs map to the `whoop_polling_*` settings
 * keys; the button enqueues the same refresh manually.
 */
function WhoopPollingSection() {
  const [enabled, setEnabled] = useState(false)
  const [refreshState, setRefreshState] = useState<'idle' | 'queued' | 'error'>('idle')
  const [interval, setIntervalMin] = useState(10)
  const [startHour, setStartHour] = useState(5)
  const [endHour, setEndHour] = useState(11)
  const [loaded, setLoaded] = useState(false)
  const saveTimer = useRef<number | null>(null)

  useEffect(() => {
    let alive = true
    void getSettings().then((s) => {
      if (!alive) return
      setEnabled(s.whoop_polling_enabled === 'true')
      setIntervalMin(Number(s.whoop_polling_interval_minutes ?? '10') || 10)
      setStartHour(Number(s.whoop_polling_window_start_hour ?? '5') || 5)
      setEndHour(Number(s.whoop_polling_window_end_hour ?? '11') || 11)
      setLoaded(true)
    })
    return () => {
      alive = false
    }
  }, [])

  // Debounce writes — sliders fire onChange per step, and the PUT endpoint is
  // rate-limited. Coalesce rapid changes into one save ~400 ms after the last.
  const persist = useCallback(
    (next: { enabled: boolean; interval: number; startHour: number; endHour: number }) => {
      if (saveTimer.current != null) window.clearTimeout(saveTimer.current)
      saveTimer.current = window.setTimeout(() => {
        void updateWhoopPolling({
          enabled: next.enabled,
          intervalMinutes: next.interval,
          windowStartHour: next.startHour,
          windowEndHour: next.endHour,
        })
      }, 400)
    },
    [],
  )

  if (!loaded) return null

  const sliderStyle: React.CSSProperties = {
    width: '100%',
    accentColor: 'var(--or-ancien)',
    cursor: 'pointer',
  }
  const labelStyle: React.CSSProperties = {
    fontFamily: 'var(--font-mono)',
    fontSize: 12,
    color: 'var(--ink-dim)',
  }

  const set = (patch: Partial<{ enabled: boolean; interval: number; startHour: number; endHour: number }>) => {
    const next = { enabled, interval, startHour, endHour, ...patch }
    setEnabled(next.enabled)
    setIntervalMin(next.interval)
    setStartHour(next.startHour)
    setEndHour(next.endHour)
    persist(next)
  }

  return (
    <>
      <Eyebrow text={'Wake trigger'} />
      <div
        style={{
          padding: '12px 14px',
          marginBottom: 14,
          background: 'var(--encre)',
          border: '1px solid var(--gilt-line)',
          fontFamily: 'var(--font-ui)',
          fontSize: 14,
          color: 'var(--ink-dim)',
          lineHeight: 1.55,
        }}
      >
        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 12 }}>
          <div style={{ color: 'var(--parchemin)', fontWeight: 600 }}>Refresh my briefing when I wake</div>
          <GoldToggle
            checked={enabled}
            onChange={(v) => set({ enabled: v })}
            ariaLabel={'Toggle WHOOP wake trigger'}
          />
        </div>
        <div style={{ marginTop: 6, fontSize: 13 }}>
          Polls WHOOP each morning and, as soon as your recovery is scored —
          shortly after you actually wake — refreshes the briefing's readiness
          card in about a minute (the narration follows in the background). If
          no briefing exists yet, the full pipeline runs instead. The fixed
          schedule stays on as a safety net.
        </div>

        {enabled && (
          <div style={{ marginTop: 14, display: 'flex', flexDirection: 'column', gap: 14 }}>
            <label style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 12 }}>
              <span style={labelStyle}>Check every</span>
              <span style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
                <TextInput
                  type="number"
                  min={1}
                  max={120}
                  value={interval}
                  onChange={(e) =>
                    set({ interval: Math.max(1, Math.min(120, Number(e.target.value) || 1)) })
                  }
                  uiSize="sm"
                  style={{ width: 56 }}
                />
                <span style={labelStyle}>min</span>
              </span>
            </label>

            <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
              <div style={{ display: 'flex', justifyContent: 'space-between' }}>
                <span style={labelStyle}>Window</span>
                <span style={{ ...labelStyle, color: 'var(--parchemin)' }}>
                  {String(startHour).padStart(2, '0')}:00 – {String(endHour).padStart(2, '0')}:00
                </span>
              </div>
              <span style={{ ...labelStyle, fontSize: 11 }}>Earliest</span>
              <input
                type="range"
                min={0}
                max={23}
                value={startHour}
                onChange={(e) => {
                  const v = Number(e.target.value)
                  set({ startHour: v, endHour: Math.max(endHour, v + 1) })
                }}
                style={sliderStyle}
                aria-label={'Window start hour'}
              />
              <span style={{ ...labelStyle, fontSize: 11 }}>Latest</span>
              <input
                type="range"
                min={1}
                max={24}
                value={endHour}
                onChange={(e) => {
                  const v = Number(e.target.value)
                  set({ endHour: v, startHour: Math.min(startHour, v - 1) })
                }}
                style={sliderStyle}
                aria-label={'Window end hour'}
              />
            </div>
          </div>
        )}

        <div style={{ marginTop: 12, display: 'flex', alignItems: 'center', gap: 10 }}>
          <GhostAction
            size="sm"
            label={refreshState === 'queued' ? 'Refresh queued…' : 'Refresh readiness now'}
            disabled={refreshState === 'queued'}
            onClick={() => {
              setRefreshState('queued')
              refreshKnowledgeHealth()
                .then(() => window.setTimeout(() => setRefreshState('idle'), 4000))
                .catch(() => setRefreshState('error'))
            }}
          />
          {refreshState === 'error' && (
            <span style={{ ...labelStyle, color: 'var(--pourpre)' }}>
              refresh failed — see engine room
            </span>
          )}
        </div>
      </div>
    </>
  )
}

/**
 * WhoopSetupBlock — the "no credentials yet" state of WhoopPanel. The user
 * pastes the client_id / client_secret from their app on
 * developer.whoop.com; on save the server persists them and we advance to
 * the "disconnected" (ready-to-connect) state.
 */
function WhoopSetupBlock({
  redirectUri,
  onSaved,
  onError,
}: {
  redirectUri: string
  onSaved: () => void
  onError: (msg: string) => void
}) {
  const [clientId, setClientId] = useState('')
  const [clientSecret, setClientSecret] = useState('')
  const [busy, setBusy] = useState(false)

  const codeStyle: React.CSSProperties = {
    fontFamily: 'var(--font-mono)',
    fontSize: 12,
    padding: '1px 5px',
    background: 'var(--well-deepest)',
    border: '1px solid var(--gilt-line)',
    color: 'var(--parchemin)',
    wordBreak: 'break-all',
  }

  const save = async () => {
    if (busy) return
    if (!clientId.trim() || !clientSecret.trim()) {
      onError('Both client ID and client secret are required.')
      return
    }
    setBusy(true)
    try {
      await uploadWhoopCredentials(clientId.trim(), clientSecret.trim())
      onSaved()
    } catch (e) {
      onError(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }

  return (
    <>
      <div style={{ color: 'var(--parchemin)', fontWeight: 600, marginBottom: 6 }}>
        Setup needed — WHOOP app credentials
      </div>
      <div style={{ marginBottom: 10 }}>
        Create an app at{' '}
        <span style={codeStyle}>developer.whoop.com</span>, then paste its
        Client ID and Client Secret below. Add this exact redirect URI to the
        app first:
      </div>
      <div style={{ ...codeStyle, display: 'block', padding: '6px 8px', marginBottom: 12 }}>
        {redirectUri || 'http://localhost:8000/api/whoop/auth/callback'}
      </div>
      <div style={{ display: 'flex', flexDirection: 'column', gap: 8, marginBottom: 12 }}>
        <TextInput
          type="text"
          value={clientId}
          placeholder="Client ID"
          onChange={(e) => setClientId(e.target.value)}
          aria-label="WHOOP Client ID"
          spellCheck={false}
          autoComplete="off"
        />
        <TextInput
          type="password"
          value={clientSecret}
          placeholder="Client Secret"
          onChange={(e) => setClientSecret(e.target.value)}
          aria-label="WHOOP Client Secret"
          spellCheck={false}
          autoComplete="off"
        />
      </div>
      <PrimaryAction
        label={busy ? 'Saving…' : 'Save credentials'}
        onClick={() => void save()}
        disabled={busy}
      />
    </>
  )
}
