import { useEffect, useState } from 'react'
import { pingHealth } from '../api/client'

/**
 * Live reachability of the local FastAPI sidecar.
 *
 * The boot probe in ``App`` only proves the sidecar was alive *once*, before
 * the splash fell away. The sidecar can still die afterwards (a crash, a manual
 * stop, a machine waking from sleep), and every panel then silently degrades to
 * em-dashes and an "Idle" shell — indistinguishable from a healthy, empty app.
 * This polls ``/health`` so the shell can surface an honest "backend down"
 * banner instead.
 *
 * Starts optimistic (``true``) and only flips to ``false`` after
 * ``FAIL_THRESHOLD`` consecutive failed probes, so a single dropped poll
 * between healthy ticks never flashes the banner; any success resets it.
 */
const FAIL_THRESHOLD = 2

export function useBackendHealth(enabled = true, pollMs = 4000): boolean {
  const [reachable, setReachable] = useState(true)

  useEffect(() => {
    if (!enabled) return
    let cancelled = false
    let fails = 0

    const probe = async () => {
      const ok = await pingHealth()
      if (cancelled) return
      if (ok) {
        fails = 0
        setReachable(true)
      } else {
        fails += 1
        if (fails >= FAIL_THRESHOLD) setReachable(false)
      }
    }

    void probe()
    const id = window.setInterval(() => void probe(), pollMs)
    return () => {
      cancelled = true
      window.clearInterval(id)
    }
  }, [enabled, pollMs])

  return reachable
}
