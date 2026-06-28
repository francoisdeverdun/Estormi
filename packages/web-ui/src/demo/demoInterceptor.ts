/**
 * Demo-mode API interceptor.
 *
 * When `VITE_DEMO_MODE` is `"true"`, this module monkey-patches
 * `window.fetch` to intercept API calls and return fictitious data.
 * No external dependency (msw, etc.) is needed.
 */

import {
  demoOverview,
  demoPipeline,
  demoBriefingList,
  demoBriefing,
  demoJobsState,
  demoJobsSchedule,
  demoSettings,
  demoDistillStatus,
  demoModelCatalog,
  demoTimeseries,
} from './sampleData'

export const DEMO_MODE =
  import.meta.env.VITE_DEMO_MODE === 'true'

/** Build a fake `Response` resolved by the patched fetch. */
function jsonResponse(data: unknown, status = 200): Response {
  return new Response(JSON.stringify(data), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

type RouteHandler = (url: URL) => Response | null

/** Static route table — path prefix → handler. Checked in order. */
const routes: Array<[string, RouteHandler]> = [
  ['/health', () => jsonResponse({ status: 'ok' })],
  ['/api/settings/overview', () => jsonResponse(demoOverview)],
  ['/api/settings', () => jsonResponse(demoSettings)],
  ['/api/pipeline', () => jsonResponse(demoPipeline)],
  ['/api/briefings/', (url) => {
    // GET /api/briefings/<date>
    const seg = url.pathname.replace('/api/briefings/', '')
    if (seg && !seg.includes('/')) {
      return jsonResponse({ ...demoBriefing, date: seg })
    }
    return null
  }],
  ['/api/briefings', () => jsonResponse(demoBriefingList)],
  ['/api/jobs/state', () => jsonResponse(demoJobsState)],
  ['/api/jobs/schedule', () => jsonResponse(demoJobsSchedule)],
  ['/api/distill/status', () => jsonResponse(demoDistillStatus)],
  ['/api/model/catalog', () => jsonResponse(demoModelCatalog)],
  ['/api/timeseries', () => jsonResponse(demoTimeseries)],
  ['/api/knowledge/sources', () => jsonResponse([])],
  ['/api/knowledge/runs', () => jsonResponse({ items: [] })],
  ['/api/knowledge/status', () => jsonResponse({ status: 'idle' })],
  ['/api/whatsapp/status', () => jsonResponse({ connected: false, paired: false, session_state: 'idle' })],
  ['/api/whoop/status', () => jsonResponse({ client: false, connected: false, redirect_uri: '' })],
  ['/api/storage/location', () => jsonResponse({ path: '/Users/demo/Estormi-data', free_bytes: 120_000_000_000, size_bytes: 60_500_000 })],
  ['/api/tts/catalog', () => jsonResponse({ models: [] })],
  ['/api/model/status', () => jsonResponse({ loaded: true, tier: 'ministral-3-14b' })],
]

/**
 * Install the fetch interceptor. Call once at app startup (before any API
 * call). Only patches when `DEMO_MODE` is true.
 */
export function installDemoInterceptor(): void {
  if (!DEMO_MODE) return

  const originalFetch = window.fetch.bind(window)

  window.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
    const url =
      typeof input === 'string'
        ? new URL(input, window.location.origin)
        : input instanceof URL
          ? input
          : new URL((input as Request).url)

    for (const [prefix, handler] of routes) {
      if (url.pathname === prefix || url.pathname.startsWith(prefix + '/') || url.pathname.startsWith(prefix + '?')) {
        const result = handler(url)
        if (result) return result
      }
    }

    // POST endpoints in demo mode return a no-op success.
    if (init?.method && init.method !== 'GET') {
      return jsonResponse({ status: 'ok (demo)' })
    }

    // Fallback: let the original fetch handle unmatched GETs (e.g. static
    // assets, fonts). Suppress network errors silently so the UI stays up.
    try {
      return await originalFetch(input, init)
    } catch {
      return jsonResponse({ error: 'demo: endpoint not mocked' }, 503)
    }
  }) as typeof window.fetch
}
