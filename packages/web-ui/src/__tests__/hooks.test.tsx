/**
 * Tests for the data hooks ``usePipeline`` and ``useSettings``.
 *
 * ``usePipeline`` fetches once on mount then polls on a fixed cadence — the
 * default 5 s comfortably respects the ≤30 polls/min cap documented for the
 * SPA (5 s → 12/min). We drive the clock with fake timers and assert the
 * exact poll interval. ``useSettings`` is a one-shot loader with a save
 * path; we assert it fetches on mount, exposes loading/error state, and
 * optimistically merges a saved patch.
 */
import { act, renderHook, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { usePipeline } from '../hooks/usePipeline'
import { useSettings } from '../hooks/useSettings'
import { writeSnapshot } from '../state/snapshotCache'

function okJson(json: unknown): Response {
  return {
    ok: true,
    status: 200,
    json: async () => json,
    text: async () => JSON.stringify(json),
  } as unknown as Response
}

describe('usePipeline', () => {
  beforeEach(() => {
    // Reset the in-memory snapshot so each test starts from the hook's initial
    // value (the test setup clears localStorage but not the process-wide cache).
    writeSnapshot('pipeline', null)
    vi.useFakeTimers()
  })
  afterEach(() => {
    vi.runOnlyPendingTimers()
    vi.useRealTimers()
    vi.restoreAllMocks()
  })

  it('fetches /api/pipeline on mount and exposes the data', async () => {
    const fetchMock = vi.fn().mockResolvedValue(okJson({ is_running: false, run_count: 2 }))
    vi.stubGlobal('fetch', fetchMock)

    const { result } = renderHook(() => usePipeline())

    // Flush the mount-time refresh microtasks.
    await act(async () => {
      await Promise.resolve()
    })
    expect(fetchMock).toHaveBeenCalledTimes(1)
    expect(fetchMock.mock.calls[0][0]).toBe('/api/pipeline')
    expect(result.current.data).toMatchObject({ run_count: 2 })
    expect(result.current.error).toBeNull()
  })

  it('polls at the documented 5 s default cadence (12/min, under the 30/min cap)', async () => {
    const fetchMock = vi.fn().mockResolvedValue(okJson({ is_running: false }))
    vi.stubGlobal('fetch', fetchMock)

    renderHook(() => usePipeline())
    await act(async () => {
      await Promise.resolve()
    })
    expect(fetchMock).toHaveBeenCalledTimes(1) // mount fetch

    // Just before the first interval — no extra poll yet.
    await act(async () => {
      vi.advanceTimersByTime(4999)
      await Promise.resolve()
    })
    expect(fetchMock).toHaveBeenCalledTimes(1)

    // Crossing 5 s fires exactly one poll; a full minute is 12 polls + mount.
    await act(async () => {
      vi.advanceTimersByTime(1)
      await Promise.resolve()
    })
    expect(fetchMock).toHaveBeenCalledTimes(2)

    await act(async () => {
      vi.advanceTimersByTime(55_000)
      await Promise.resolve()
    })
    // mount + 12 polls in the 60 s window.
    expect(fetchMock).toHaveBeenCalledTimes(13)
    expect(fetchMock.mock.calls.length).toBeLessThanOrEqual(13) // ≤30/min cap honoured
  })

  it('honours a custom pollMs', async () => {
    const fetchMock = vi.fn().mockResolvedValue(okJson({ is_running: false }))
    vi.stubGlobal('fetch', fetchMock)
    renderHook(() => usePipeline(10_000))
    await act(async () => {
      await Promise.resolve()
    })
    await act(async () => {
      vi.advanceTimersByTime(9999)
      await Promise.resolve()
    })
    expect(fetchMock).toHaveBeenCalledTimes(1)
    await act(async () => {
      vi.advanceTimersByTime(1)
      await Promise.resolve()
    })
    expect(fetchMock).toHaveBeenCalledTimes(2)
  })

  it('clears the interval on unmount (no polls fire afterwards)', async () => {
    const fetchMock = vi.fn().mockResolvedValue(okJson({ is_running: false }))
    vi.stubGlobal('fetch', fetchMock)
    const { unmount } = renderHook(() => usePipeline())
    await act(async () => {
      await Promise.resolve()
    })
    unmount()
    await act(async () => {
      vi.advanceTimersByTime(30_000)
      await Promise.resolve()
    })
    expect(fetchMock).toHaveBeenCalledTimes(1) // only the mount fetch
  })

  it('surfaces an error string when the fetch fails', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({ ok: false, status: 500 } as unknown as Response),
    )
    const { result } = renderHook(() => usePipeline())
    await act(async () => {
      await Promise.resolve()
      await Promise.resolve()
    })
    expect(result.current.error).toContain('/api/pipeline')
  })
})

describe('useSettings', () => {
  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('loads /api/settings on mount and flips loading false', async () => {
    const fetchMock = vi.fn().mockResolvedValue(okJson({ theme: 'dark' }))
    vi.stubGlobal('fetch', fetchMock)

    const { result } = renderHook(() => useSettings())
    expect(result.current.loading).toBe(true)

    await waitFor(() => expect(result.current.loading).toBe(false))
    expect(fetchMock.mock.calls[0][0]).toBe('/api/settings')
    expect(result.current.settings).toEqual({ theme: 'dark' })
    expect(result.current.error).toBeNull()
  })

  it('exposes an error when the load fails', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({ ok: false, status: 500 } as unknown as Response),
    )
    const { result } = renderHook(() => useSettings())
    await waitFor(() => expect(result.current.loading).toBe(false))
    expect(result.current.error).toBeTruthy()
    expect(result.current.settings).toBeNull()
  })

  it('save() PUTs the patch and optimistically merges the response', async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(okJson({ theme: 'dark', lang: 'en' })) // mount load
      .mockResolvedValueOnce(okJson({ theme: 'light', lang: 'en' })) // save response
    vi.stubGlobal('fetch', fetchMock)

    const { result } = renderHook(() => useSettings())
    await waitFor(() => expect(result.current.loading).toBe(false))

    await act(async () => {
      await result.current.save({ theme: 'light' })
    })

    const saveCall = fetchMock.mock.calls[1]
    expect(saveCall[0]).toBe('/api/settings')
    expect((saveCall[1] as RequestInit).method).toBe('PUT')
    expect(result.current.settings).toEqual({ theme: 'light', lang: 'en' })
  })
})
