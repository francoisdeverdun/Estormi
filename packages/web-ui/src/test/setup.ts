/**
 * Vitest setup — runs once before each test file.
 *
 * happy-dom v20 exposes a ``localStorage`` object but without the Storage
 * prototype methods (`getItem` / `setItem` / `removeItem` / `clear`), so we
 * install a minimal in-memory polyfill. That mirrors browser behaviour
 * closely enough for the snapshot cache to round-trip values under test.
 *
 * The map is replaced per-test (via the ``beforeEach`` hook) so cached
 * values never leak across cases.
 */
import '@testing-library/jest-dom/vitest'
import { afterEach, beforeEach } from 'vitest'
import { cleanup } from '@testing-library/react'

function installLocalStoragePolyfill(): void {
  const store = new Map<string, string>()
  const polyfill: Storage = {
    get length() {
      return store.size
    },
    clear() {
      store.clear()
    },
    getItem(key: string) {
      return store.has(key) ? store.get(key)! : null
    },
    setItem(key: string, value: string) {
      store.set(String(key), String(value))
    },
    removeItem(key: string) {
      store.delete(key)
    },
    key(index: number) {
      return Array.from(store.keys())[index] ?? null
    },
  }
  // Define on globalThis + window so both `localStorage` and
  // `window.localStorage` paths in the cache code resolve to the same impl.
  Object.defineProperty(globalThis, 'localStorage', {
    value: polyfill,
    configurable: true,
    writable: true,
  })
  if (typeof window !== 'undefined') {
    Object.defineProperty(window, 'localStorage', {
      value: polyfill,
      configurable: true,
      writable: true,
    })
  }
}

beforeEach(() => {
  installLocalStoragePolyfill()
})

afterEach(() => {
  cleanup()
})
