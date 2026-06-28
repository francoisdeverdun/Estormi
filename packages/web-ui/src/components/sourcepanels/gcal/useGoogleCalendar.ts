/**
 * useGoogleCalendar — the connection state machine behind GoogleCalendarPanel.
 *
 * A two-step probe drives the state: GET /api/calendar/auth/url (a 400 means
 * client secrets are missing → 'setup'), then GET /api/google-calendar/calendars
 * (200 → 'connected', 401 → 'disconnected'). Owns the OAuth open/poll,
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
    // 1. Probe the auth-url endpoint: 400 → missing client secrets ('setup'),
    //    success → secrets present (a token may or may not exist yet).
    try {
      await getGoogleAuthUrl()
    } catch (e) {
      if (e instanceof ApiError && e.status === 400) return 'setup'
      throw e
    }
    // 2. Try the calendars list: 200 → connected (cache the list), 401 → need
    //    to connect, anything else → surface as error (thrown → 'error').
    try {
      const list = await getGoogleCalendars()
      setCalendars(list)
      return 'connected'
    } catch (e) {
      if (e instanceof ApiError && e.status === 401) return 'disconnected'
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
