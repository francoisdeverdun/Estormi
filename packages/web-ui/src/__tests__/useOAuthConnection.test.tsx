/**
 * Tests for the shared OAuth connection state machine (useOAuthConnection),
 * the hook WhoopPanel and useGoogleCalendar both build on. We cover the four
 * behaviours that used to be duplicated (and untested) in each panel: the
 * mount probe, the resolved-state mapping, error capture + clearing, and the
 * poll-every-4s-while-'disconnected' loop that flips the panel to 'connected'
 * the moment the browser OAuth callback lands.
 */
import { act, renderHook } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { useOAuthConnection } from '../components/sourcepanels/useOAuthConnection'

async function flush() {
  // Two microtask rounds drain the probe's await chain (await probeFn → setState).
  await act(async () => {
    await Promise.resolve()
    await Promise.resolve()
  })
}

describe('useOAuthConnection', () => {
  beforeEach(() => {
    vi.useFakeTimers()
  })
  afterEach(() => {
    vi.runOnlyPendingTimers()
    vi.useRealTimers()
    vi.restoreAllMocks()
  })

  it('probes on mount and adopts the resolved state', async () => {
    const probeFn = vi.fn().mockResolvedValue('connected' as const)
    const { result } = renderHook(() => useOAuthConnection(probeFn))
    expect(result.current.state).toBe('probing')
    await flush()
    expect(probeFn).toHaveBeenCalledTimes(1)
    expect(result.current.state).toBe('connected')
    expect(result.current.error).toBeNull()
  })

  it('captures a probe failure as the error state', async () => {
    const probeFn = vi.fn().mockRejectedValue(new Error('boom'))
    const { result } = renderHook(() => useOAuthConnection(probeFn))
    await flush()
    expect(result.current.state).toBe('error')
    expect(result.current.error).toBe('boom')
  })

  it('clears a prior error once a later probe succeeds', async () => {
    const probeFn = vi
      .fn()
      .mockRejectedValueOnce(new Error('boom'))
      .mockResolvedValue('setup' as const)
    const { result } = renderHook(() => useOAuthConnection(probeFn))
    await flush()
    expect(result.current.state).toBe('error')

    await act(async () => {
      await result.current.probe()
    })
    expect(result.current.state).toBe('setup')
    expect(result.current.error).toBeNull()
  })

  it('polls every 4s while disconnected, then stops once connected', async () => {
    const probeFn = vi
      .fn()
      .mockResolvedValueOnce('disconnected' as const) // mount
      .mockResolvedValueOnce('disconnected' as const) // first poll
      .mockResolvedValue('connected' as const) // second poll → connected
    const { result } = renderHook(() => useOAuthConnection(probeFn))
    await flush()
    expect(result.current.state).toBe('disconnected')
    expect(probeFn).toHaveBeenCalledTimes(1)

    // Just before 4s: no extra poll.
    await act(async () => {
      vi.advanceTimersByTime(3999)
      await Promise.resolve()
    })
    expect(probeFn).toHaveBeenCalledTimes(1)

    // Crossing 4s fires one poll (still disconnected).
    await act(async () => {
      vi.advanceTimersByTime(1)
      await Promise.resolve()
      await Promise.resolve()
    })
    expect(probeFn).toHaveBeenCalledTimes(2)
    expect(result.current.state).toBe('disconnected')

    // Next interval flips to connected and the poll loop must stop.
    await act(async () => {
      vi.advanceTimersByTime(4000)
      await Promise.resolve()
      await Promise.resolve()
    })
    expect(result.current.state).toBe('connected')
    const callsAtConnect = probeFn.mock.calls.length
    await act(async () => {
      vi.advanceTimersByTime(12000)
      await Promise.resolve()
    })
    expect(probeFn.mock.calls.length).toBe(callsAtConnect)
  })

  it('does not poll in a non-disconnected state', async () => {
    const probeFn = vi.fn().mockResolvedValue('connected' as const)
    renderHook(() => useOAuthConnection(probeFn))
    await flush()
    expect(probeFn).toHaveBeenCalledTimes(1)
    await act(async () => {
      vi.advanceTimersByTime(20000)
      await Promise.resolve()
    })
    expect(probeFn).toHaveBeenCalledTimes(1)
  })
})
