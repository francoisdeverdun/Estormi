/**
 * Tests for useGoogleCalendar's connection probe ordering.
 *
 * Regression guard: a live OAuth token lives in the system keyring and carries
 * its own client_secret, so it round-trips even when google_client_secrets.json
 * is absent from the data dir. The probe must therefore check the authoritative
 * calendars endpoint FIRST and report 'connected' regardless of that file —
 * before, it probed auth/url first and a missing-secrets 400 short-circuited a
 * working connection to 'setup'.
 */
import { act, renderHook } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { ApiError } from '../api/client'
import { useGoogleCalendar } from '../components/sourcepanels/gcal/useGoogleCalendar'
import { getGoogleAuthUrl, getGoogleCalendars } from '../api/sources_ext'

vi.mock('../api/sources_ext', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api/sources_ext')>()
  return { ...actual, getGoogleCalendars: vi.fn(), getGoogleAuthUrl: vi.fn() }
})

const calendarsMock = vi.mocked(getGoogleCalendars)
const authUrlMock = vi.mocked(getGoogleAuthUrl)

async function flush() {
  // Two microtask rounds drain the probe's await chain (await probeFn → setState).
  await act(async () => {
    await Promise.resolve()
    await Promise.resolve()
  })
}

describe('useGoogleCalendar probe ordering', () => {
  beforeEach(() => {
    vi.useFakeTimers()
  })
  afterEach(() => {
    vi.runOnlyPendingTimers()
    vi.useRealTimers()
    vi.restoreAllMocks()
  })

  it("reports 'connected' from a live token even when client secrets are missing", async () => {
    // Token works → calendars list resolves. auth/url would 400 (no secrets
    // file), but it must never be consulted once the token round-trips.
    calendarsMock.mockResolvedValue([
      { id: 'a@b.com', name: 'a@b.com', selected: true, group_type: 'unknown' },
    ])
    authUrlMock.mockRejectedValue(new ApiError(400, 'no secrets'))

    const { result } = renderHook(() => useGoogleCalendar())
    await flush()

    expect(result.current.state).toBe('connected')
    expect(result.current.calendars).toHaveLength(1)
    expect(authUrlMock).not.toHaveBeenCalled()
  })

  it("reports 'setup' when there is no token and no client secrets", async () => {
    calendarsMock.mockRejectedValue(new ApiError(401, 'not authenticated'))
    authUrlMock.mockRejectedValue(new ApiError(400, 'no secrets'))

    const { result } = renderHook(() => useGoogleCalendar())
    await flush()

    expect(result.current.state).toBe('setup')
  })

  it("reports 'disconnected' when secrets exist but no token yet", async () => {
    calendarsMock.mockRejectedValue(new ApiError(401, 'not authenticated'))
    authUrlMock.mockResolvedValue({ url: 'https://accounts.google', state: 'x' })

    const { result } = renderHook(() => useGoogleCalendar())
    await flush()

    expect(result.current.state).toBe('disconnected')
  })
})
