/**
 * Snapshot cache — stale-while-revalidate for page + modal data.
 *
 * Two layers:
 *   1. Process-lifetime ``Map`` (free, instant) — survives navigation.
 *   2. ``localStorage`` mirror (per-key) — survives hard reload + app restart.
 *
 * Every modal opens with whatever was last seen (0ms perceived load), then
 * the background fetch reconciles. The user never sees an empty "Loading…"
 * for a panel they've already opened before.
 *
 * Values must be JSON-serialisable. Per-key size is capped at ~64 KB so a
 * misbehaving caller can't blow out the storage quota — bigger payloads
 * fall back to memory-only and won't survive a reload.
 *
 * The cache is also a *live store*: every write notifies the per-key
 * subscribers below, so all `useSnapshotState` consumers of a key re-render
 * when any writer updates it — a background poll, a prefetch, or another
 * panel. Without this, a key shared by two components silently desyncs (one
 * polls, the other froze its value at mount) and only a reload reconciled it.
 */
import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type Dispatch,
  type SetStateAction,
} from 'react'

const cache = new Map<string, unknown>()

/** Per-key subscriber callbacks, fired on every write/clear of that key. */
const subscribers = new Map<string, Set<() => void>>()

function notify(key: string): void {
  const subs = subscribers.get(key)
  if (!subs) return
  for (const cb of subs) cb()
}

const LS_PREFIX = 'estormi.snap.v1.'
const LS_MAX_BYTES = 65_536 // 64 KB per key — covers every modal's payload.

function lsKey(key: string): string {
  return LS_PREFIX + key
}

function tryReadLocalStorage<T>(key: string): T | undefined {
  if (typeof window === 'undefined') return undefined
  try {
    const raw = window.localStorage.getItem(lsKey(key))
    if (raw == null) return undefined
    return JSON.parse(raw) as T
  } catch {
    return undefined
  }
}

function tryWriteLocalStorage<T>(key: string, value: T): void {
  if (typeof window === 'undefined') return
  try {
    const raw = JSON.stringify(value)
    if (raw.length > LS_MAX_BYTES) {
      // Payload too big — drop the stale key so we don't keep returning it.
      window.localStorage.removeItem(lsKey(key))
      return
    }
    window.localStorage.setItem(lsKey(key), raw)
  } catch {
    // QuotaExceededError, JSON-circular, etc. — best-effort.
  }
}

export function readSnapshot<T>(key: string): T | undefined {
  const mem = cache.get(key) as T | undefined
  if (mem !== undefined) return mem
  const ls = tryReadLocalStorage<T>(key)
  if (ls !== undefined) {
    cache.set(key, ls) // promote to memory so subsequent reads skip JSON parse.
    return ls
  }
  return undefined
}

export function writeSnapshot<T>(key: string, value: T): void {
  cache.set(key, value)
  tryWriteLocalStorage(key, value)
  notify(key)
}

/**
 * useState that survives page navigation AND hard reload, and stays in sync
 * across every consumer of the same key. Drop-in replacement for ``useState``:
 * supports both value and functional updaters, identical signature.
 *
 * Reactivity: the hook subscribes to its key, so a write from anywhere — this
 * component, a sibling panel, a background poll, a prefetch — re-renders it
 * with the fresh value. That is what gives the dashboard its hot reload: the
 * Summarium's 5s overview poll now flows straight into the Sources panel
 * counts without a manual refresh.
 */
export function useSnapshotState<T>(
  key: string,
  initial: T,
): [T, Dispatch<SetStateAction<T>>] {
  const [value, setValue] = useState<T>(() => {
    const cached = readSnapshot<T>(key)
    return cached !== undefined ? cached : initial
  })

  // Mirror the latest value for the setter, so a functional update resolves
  // against the current value without re-subscribing on every change.
  const valueRef = useRef(value)
  valueRef.current = value

  useEffect(() => {
    const sync = () => {
      const cached = readSnapshot<T>(key)
      // Identical references no-op in React; a changed value re-renders.
      if (cached !== undefined) setValue(cached)
    }
    // A write may have landed between this hook's first render and this
    // commit (e.g. a sibling wrote first) — reconcile immediately.
    sync()
    let subs = subscribers.get(key)
    if (!subs) {
      subs = new Set()
      subscribers.set(key, subs)
    }
    subs.add(sync)
    return () => {
      subs.delete(sync)
      if (subs.size === 0) subscribers.delete(key)
    }
  }, [key])

  // Single write path: persist + broadcast. `notify` mirrors the new value
  // back into this component's own state via its subscription, so we don't
  // call setValue here — the cache stays the one source of truth.
  const set: Dispatch<SetStateAction<T>> = useCallback(
    (next) => {
      const resolved =
        typeof next === 'function'
          ? (next as (p: T) => T)(valueRef.current)
          : next
      writeSnapshot(key, resolved)
    },
    [key],
  )

  return [value, set]
}
