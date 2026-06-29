/**
 * useOAuthConnection — the connection state machine shared by the two OAuth
 * source panels (WHOOP and Google Calendar).
 *
 * Both panels do exactly the same three things: probe a status endpoint on
 * mount, poll every 4 s while 'disconnected' so the panel flips to 'connected'
 * the instant the browser OAuth callback lands, and surface any probe failure
 * as 'error'. Only the probe *body* differs (one endpoint vs two, plus
 * source-specific side data like the redirect URI or the calendar list), so it
 * is passed in: `probeFn` returns the resolved {@link OAuthConnState} (setting
 * its own side data as it goes) and throws to signal 'error'.
 */
import { useCallback, useEffect, useState } from 'react'

export type OAuthConnState =
  | 'probing'
  | 'setup'
  | 'disconnected'
  | 'connected'
  | 'error'

export interface OAuthConnection {
  state: OAuthConnState
  error: string | null
  setState: (s: OAuthConnState) => void
  setError: (e: string | null) => void
  probe: () => Promise<void>
}

const POLL_INTERVAL_MS = 4000

export function useOAuthConnection(probeFn: () => Promise<OAuthConnState>): OAuthConnection {
  const [state, setState] = useState<OAuthConnState>('probing')
  const [error, setError] = useState<string | null>(null)

  const probe = useCallback(async () => {
    try {
      const next = await probeFn()
      setState(next)
      setError(null)
    } catch (e) {
      setState('error')
      setError(e instanceof Error ? e.message : String(e))
    }
  }, [probeFn])

  useEffect(() => {
    void probe()
  }, [probe])

  // While the user is finishing consent in their browser, poll so the panel
  // flips to 'connected' the moment the callback lands.
  useEffect(() => {
    if (state !== 'disconnected') return
    const id = window.setInterval(() => void probe(), POLL_INTERVAL_MS)
    return () => window.clearInterval(id)
  }, [state, probe])

  return { state, error, setState, setError, probe }
}
