import type { Page, Route } from '@playwright/test'

/**
 * Backend stubs for the hermetic e2e run.
 *
 * The SPA talks to a FastAPI sidecar over ``/api/**`` + ``/health`` + an
 * ``/api/events`` SSE stream. None of that runs under ``vite preview``, so we
 * intercept every backend request and answer with realistic fixture JSON.
 * Anything not matched by a specific handler falls through to a generic
 * ``{}`` so a stray fetch can never hang the boot splash.
 */

const overview = {
  version: '0.1.0',
  data_dir: '/Users/test/Library/Estormi',
  settings: { knowledge_llm_provider: 'local' },
  storage: {
    db_bytes: 5 * 1024 * 1024,
    qdrant_bytes: 2 * 1024 * 1024,
    staging_bytes: 0,
    total_chunks: 1234,
  },
  sources: { counts: { notes: 100, mail: 50 }, watermarks: {} },
  model: { name: 'mistral', loaded: true, exists: true, size_bytes: 0 },
  mcp: { token: 't', port: '8000', bind_address: '127.0.0.1' },
  pipeline: {
    next_run_at: '—',
    last_run_started: '—',
    overall_status: 'ok',
    last_run_failed_stages: [],
  },
  permissions: { imessage_fda: true },
}

const pipeline = {
  is_running: false,
  overall_status: 'ok',
  last_run_started: '2026-06-03 04:00',
  last_run_ended: '2026-06-03 04:12',
  last_run_duration_s: 720,
  last_run_duration: '12m',
  last_run_ago: '1 day ago',
  last_run_failed_stages: [],
  mean_duration_s: 700,
  mean_duration: '11m 40s',
  next_run_at: '2026-06-05 04:00',
  run_count: 42,
  errors: [],
  stages: [],
  history: [],
}

const briefings = {
  items: [
    {
      date: '2026-06-04',
      title: 'Briefing — 4 June 2026',
      generatedAt: '2026-06-04T07:00:00Z',
      sourceCount: 6,
      videoCount: 2,
    },
  ],
}

const briefingBody = {
  date: '2026-06-04',
  title: 'Briefing — 4 June 2026',
  htmlBody: '<h1>Today</h1><p>Stubbed briefing body for the e2e flow.</p>',
}

const timeseries = {
  days: ['2026-06-03', '2026-06-04'],
  sources: ['notes', 'mail'],
  series: [
    { day: '2026-06-03', total: 1200, by_source: { notes: 800, mail: 400 } },
    { day: '2026-06-04', total: 1234, by_source: { notes: 820, mail: 414 } },
  ],
}

// GET /api/jobs/schedule — the engine room's UPCOMING section reads this when
// the popover opens; whoopWake must be present or UpcomingSection dereferences
// undefined.windowStartHour and crashes the render.
const jobsSchedule = {
  crons: [],
  whoopWake: {
    enabled: false,
    windowStartHour: 6,
    windowEndHour: 9,
    lastFiredDate: '',
    nextCheck: null,
  },
}

// GET /api/distill/status — the distillation engine is idle/uninstalled in the
// e2e fixture. Shape mirrors DistillStatus (src/api/distill.ts); the inner
// ``status`` object must be present or DistillationCard crashes the render.
const distillStatus = {
  status: { phase: 'idle' },
  references: { days: [], count: 0, models: {} },
  tooling: { python: '3.12', mlx_lm: '', quantize: '', convert: '', ready: false },
  installed: false,
  installedFile: '',
  running: [],
}

function json(route: Route, body: unknown, status = 200) {
  return route.fulfill({
    status,
    contentType: 'application/json',
    body: JSON.stringify(body),
  })
}

/**
 * Install all backend route stubs on a page. Call before navigating.
 *
 * Playwright gives *later*-registered handlers precedence, so the broad
 * ``/api/**`` catch-all is registered FIRST and the specific handlers after,
 * letting them win.
 */
export async function stubBackend(page: Page): Promise<void> {
  await page.route('**/health', (r) => r.fulfill({ status: 200, body: 'ok' }))

  // Broad catch-all (lowest precedence) — never let an unhandled fetch hang
  // the boot splash. The path-keyed handler below answers the typed shapes.
  await page.route('**/api/**', (route) => {
    const path = new URL(route.request().url()).pathname

    if (path.endsWith('/api/settings/overview')) return json(route, overview)
    if (path.endsWith('/api/settings')) return json(route, overview.settings)
    if (path.endsWith('/api/pipeline')) return json(route, pipeline)
    if (path.endsWith('/api/briefings')) return json(route, briefings)
    if (/\/api\/briefings\/\d{4}-\d{2}-\d{2}$/.test(path)) return json(route, briefingBody)
    if (path.endsWith('/api/timeseries')) return json(route, timeseries)
    if (path.endsWith('/api/permissions')) return json(route, { sources: [], volumes: [] })
    // Array-typed reads — these consumers call ``.length`` / ``.map`` on the
    // payload, so the fallback must not be an object.
    if (path.endsWith('/api/knowledge/sources')) return json(route, [])
    if (path.endsWith('/api/knowledge/llm-models'))
      return json(route, { models: [], current: '', setting_key: 'briefing_model_tier' })
    if (path.endsWith('/api/model/catalog'))
      return json(route, { models: [], selection: { briefing: 'small' }, defaults: { briefing: 'small' } })
    // TTS narration catalog — the Officina card maps over ``.models`` at boot,
    // so the shape must carry the array (a bare ``{}`` white-screens the app).
    if (path.endsWith('/api/tts/catalog')) return json(route, { models: [], selected: '' })
    // Distillation status — DistillationCard reads ``status.status.phase`` (and
    // destructures references/tooling/installed); a bare ``{}`` leaves the inner
    // ``status`` undefined and white-screens the whole app, so carry the shape.
    if (path.endsWith('/api/distill/status')) return json(route, distillStatus)
    if (path.endsWith('/api/jobs/schedule')) return json(route, jobsSchedule)

    return json(route, {})
  })

  // SSE engine-event stream (registered last → wins over the catch-all). An
  // empty stream lets the EventSource bridge attach without a MIME error.
  await page.route('**/api/events', (r) =>
    r.fulfill({
      status: 200,
      headers: { 'content-type': 'text/event-stream', 'cache-control': 'no-cache' },
      body: ':\n\n',
    }),
  )
}
