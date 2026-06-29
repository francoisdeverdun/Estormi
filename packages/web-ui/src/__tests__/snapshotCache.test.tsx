/**
 * Tests for ``state/snapshotCache`` — ``readSnapshot`` / ``writeSnapshot``:
 * the memory + ``localStorage`` mirror.
 *
 * The setup file clears ``localStorage`` between tests so cached values
 * never leak across cases.
 */
import { act, renderHook } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import {
  readSnapshot,
  useSnapshotState,
  writeSnapshot,
} from '../state/snapshotCache'

describe('snapshotCache', () => {
  it('round-trips a value through memory + localStorage', () => {
    writeSnapshot('demo.key', { hello: 'world' })
    expect(readSnapshot('demo.key')).toEqual({ hello: 'world' })
    // The value is also serialised into the localStorage mirror.
    const stored = globalThis.localStorage.getItem('estormi.snap.v1.demo.key')
    expect(stored).toBe('{"hello":"world"}')
  })

  it('skips localStorage for oversized payloads but keeps memory copy', () => {
    // Generate a payload larger than the 64 KB cap.
    const big = { blob: 'x'.repeat(70_000) }
    writeSnapshot('demo.big', big)
    // Memory copy still works.
    expect(readSnapshot('demo.big')).toEqual(big)
    // Nothing stored — the cap rejects it.
    expect(globalThis.localStorage.getItem('estormi.snap.v1.demo.big')).toBeNull()
  })

  describe('useSnapshotState reactivity', () => {
    it('re-renders every consumer of a key when one writes', () => {
      // Two independent hooks on the same key model two panels (e.g. the
      // Summarium poll and the Sources panel) — the regression was that the
      // second froze at its mount value.
      const a = renderHook(() => useSnapshotState<number>('demo.live', 0))
      const b = renderHook(() => useSnapshotState<number>('demo.live', 0))

      act(() => a.result.current[1](7))

      expect(a.result.current[0]).toBe(7)
      expect(b.result.current[0]).toBe(7) // would stay 0 without the subscription
    })

    it('re-renders on a direct writeSnapshot (a background poll)', () => {
      const { result } = renderHook(() => useSnapshotState<string>('demo.poll', 'old'))
      act(() => writeSnapshot('demo.poll', 'fresh'))
      expect(result.current[0]).toBe('fresh')
    })

    it('resolves functional updates against the current value', () => {
      const { result } = renderHook(() => useSnapshotState<number>('demo.fn', 1))
      act(() => result.current[1]((n) => n + 4))
      expect(result.current[0]).toBe(5)
    })

    it('drops the subscriber set once the last consumer unmounts', () => {
      const first = renderHook(() => useSnapshotState<number>('demo.gc', 0))
      const second = renderHook(() => useSnapshotState<number>('demo.gc', 0))
      first.unmount()
      second.unmount()
      // A write with no live consumers must not throw.
      expect(() => writeSnapshot('demo.gc', 9)).not.toThrow()
    })
  })
})
