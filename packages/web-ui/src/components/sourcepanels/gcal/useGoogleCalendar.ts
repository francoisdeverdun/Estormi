/**
 * useGoogleCalendar — the connection state machine behind GoogleCalendarPanel.
 *
 * A two-step probe drives the state. The authoritative check comes first: GET
 * /api/google-calendar/calendars (200 → 'connected'). A live OAuth token —
 * which lives in the system keyring and carries its own client_secret — works
 * even when google_client_secrets.json is absent from the data dir, so an
 * existing connection must report 'connected' regardless of that file. Only
 * when no token is present (401) do we fall back to GET /api/calendar/auth/url
 * to distinguish 'setup' (400 → client secrets missing) from 'disconnected'
 * (200 → secrets present, not yet authorized). Owns the OAuth open/poll,
 * disconnect, and per-calendar selection / life-context tagging. Extracted from
 * GoogleCalendarPanel.tsx.
 */
import { useCallback, useState } from 'react'
import { apiSend, ApiError } from '../../../api/client'
import { useOAuthConnection, type OAuthConnState } from '../useOAuthConnection'
import {
  disconnectCalendarOAuth,
  getGoogleAuthUrl,
  getGoogleCalendars,
  setGoogleCalendarGroupType,
  setGoogleCalendarSelected,
  type GCalCalendar,
  type GCalGroupType,
} from '../../../api/sources_ext'

export function useGoogleCalendar(onChanged?: () => void) {
  const [calendars, setCalendars] = useState<GCalCalendar[] | null>(null)

  const probeFn = useCallback(async (): Promise<OAuthConnState> => {
    // 1. Authoritative: does a working OAuth token exist? The token lives in the
    //    system keyring and carries its own client_secret, so it round-trips
    //    even when google_client_secrets.json is missing from the data dir.
    //    200 → connected (cache the list); 401 → no token, fall through.
    try {
      const list = await getGoogleCalendars()
      setCalendars(list)
      return 'connected'
    } catch (e) {
      if (!(e instanceof ApiError) || e.status !== 401) throw e
    }
    // 2. No token. Probe the auth-url endpoint to tell the two unauthenticated
    //    states apart: 400 → client secrets missing ('setup'), 200 → secrets
    //    present but not yet authorized ('disconnected').
    try {
      await getGoogleAuthUrl()
      return 'disconnected'
    } catch (e) {
      if (e instanceof ApiError && e.status === 400) return 'setup'
      throw e
    }
  }, [])

  const { state, error, setState, setError, probe } = useOAuthConnection(probeFn)

  const startConnect = async () => {
    try {
      await apiSend('/api/calendar/auth/open', 'POST')
      setError(null)
      // Probe immediately and keep polling — the user is now in their
      // browser completing the consent screen.
      void probe()
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    }
  }

  const disconnect = async () => {
    try {
      await disconnectCalendarOAuth()
      setCalendars(null)
      setState('disconnected')
      onChanged?.()
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    }
  }

  const toggleCalendar = async (id: string, selected: boolean) => {
    try {
      await setGoogleCalendarSelected(id, selected)
      setCalendars(
        (prev) => prev?.map((c) => (c.id === id ? { ...c, selected } : c)) ?? null,
      )
      onChanged?.()
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    }
  }

  const setCalendarGroup = async (id: string, group_type: GCalGroupType) => {
    try {
      await setGoogleCalendarGroupType(id, group_type)
      setCalendars(
        (prev) => prev?.map((c) => (c.id === id ? { ...c, group_type } : c)) ?? null,
      )
      onChanged?.()
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    }
  }

  return {
    state,
    error,
    calendars,
    setError,
    probe,
    startConnect,
    disconnect,
    toggleCalendar,
    setCalendarGroup,
  }
}
