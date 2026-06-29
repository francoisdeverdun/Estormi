/**
 * Tests for the typed API clients layered on ``client.ts``
 * (``overview.ts`` / ``pipeline.ts`` / ``settings.ts`` / ``knowledge.ts`` /
 * ``sources_ext.ts`` / ``model.ts``).
 *
 * The base client is covered separately in ``apiClient.test.ts``; here we
 * assert each wrapper hits the right URL with the right verb, URL-encodes
 * path segments / query params, and threads its body through. ``fetch`` is
 * mocked per-test and we read back the call args.
 */
import { afterEach, describe, expect, it, vi } from 'vitest'
import { getOverview } from '../api/overview'
import { getPipeline, runPipeline, stopPipeline } from '../api/pipeline'
import {
  getKnowledgeLlmModels,
  getKnowledgeSources,
  putKnowledgeSources,
  recheckFda,
  resetSourceData,
  resetWatermark,
  resetWhatsAppLog,
  resolveKnowledgeSource,
  toggleSource,
  updateSettings,
  updateWhoopPolling,
} from '../api/settings'
import {
  deleteBriefing,
  getBriefing,
  listBriefings,
  resetBriefings,
  runKnowledge,
} from '../api/knowledge'
import {
  disconnectCalendarOAuth,
  disconnectWhoop,
  getGoogleAuthUrl,
  getGoogleCalendars,
  getWhatsAppChats,
  getWhoopStatus,
  openWhoopAuth,
  patchWhatsAppChat,
  pickFolder,
  resetGoogleCalendarSyncToken,
  resetWhatsAppPairing,
  setGoogleCalendarGroupType,
  setGoogleCalendarSelected,
  uploadGoogleClientSecrets,
  uploadWhoopCredentials,
  whatsappQrUrl,
} from '../api/sources_ext'
import { deleteModel, getModelCatalog } from '../api/model'

function okJson(json: unknown): Response {
  return {
    ok: true,
    status: 200,
    json: async () => json,
    text: async () => JSON.stringify(json),
  } as unknown as Response
}

function mockFetch(json: unknown = {}) {
  const fetchMock = vi.fn().mockResolvedValue(okJson(json))
  vi.stubGlobal('fetch', fetchMock)
  return fetchMock
}

/** Pull the (url, init) tuple from the most recent fetch call. */
function lastCall(fetchMock: ReturnType<typeof vi.fn>): [string, RequestInit] {
  const [url, init] = fetchMock.mock.calls[fetchMock.mock.calls.length - 1]
  return [url as string, (init ?? {}) as RequestInit]
}

afterEach(() => {
  vi.restoreAllMocks()
})

describe('overview client', () => {
  it('getOverview → GET /api/settings/overview', async () => {
    const f = mockFetch({ version: '1' })
    const out = await getOverview()
    expect((out as unknown as { version: string }).version).toBe('1')
    expect(lastCall(f)[0]).toBe('/api/settings/overview')
  })
})

describe('pipeline client', () => {
  it('getPipeline → GET /api/pipeline', async () => {
    const f = mockFetch({ is_running: false })
    await getPipeline()
    expect(lastCall(f)[0]).toBe('/api/pipeline')
    expect(lastCall(f)[1].method ?? 'GET').toBe('GET')
  })

  it('runPipeline with no stage → bare POST /api/pipeline/run', async () => {
    const f = mockFetch({ status: 'queued' })
    await runPipeline()
    const [url, init] = lastCall(f)
    expect(url).toBe('/api/pipeline/run')
    expect(init.method).toBe('POST')
  })

  it('runPipeline(stage) URL-encodes the stage query param', async () => {
    const f = mockFetch({ status: 'queued' })
    await runPipeline('apple notes')
    expect(lastCall(f)[0]).toBe('/api/pipeline/run?stage=apple%20notes')
  })

  it('stopPipeline → POST /api/pipeline/stop', async () => {
    const f = mockFetch({ status: 'stopping' })
    await stopPipeline()
    const [url, init] = lastCall(f)
    expect(url).toBe('/api/pipeline/stop')
    expect(init.method).toBe('POST')
  })
})

describe('settings client', () => {
  it('updateSettings → PUT /api/settings with the patch body', async () => {
    const f = mockFetch({ a: '1' })
    await updateSettings({ a: '1' })
    const [url, init] = lastCall(f)
    expect(url).toBe('/api/settings')
    expect(init.method).toBe('PUT')
    expect(init.body).toBe(JSON.stringify({ a: '1' }))
  })

  it('updateWhoopPolling stringifies every knob into the settings patch', async () => {
    const f = mockFetch({})
    await updateWhoopPolling({
      enabled: true,
      intervalMinutes: 30,
      windowStartHour: 6,
      windowEndHour: 22,
    })
    const body = JSON.parse(lastCall(f)[1].body as string)
    expect(body).toEqual({
      whoop_polling_enabled: 'true',
      whoop_polling_interval_minutes: '30',
      whoop_polling_window_start_hour: '6',
      whoop_polling_window_end_hour: '22',
    })
  })

  it('getKnowledgeLlmModels URL-encodes the provider query', async () => {
    const f = mockFetch({ models: [], current: '', setting_key: 'x' })
    await getKnowledgeLlmModels('claude-cli')
    expect(lastCall(f)[0]).toBe('/api/knowledge/llm-models?provider=claude-cli')
  })

  it('toggleSource encodes the name and sends the enabled flag', async () => {
    const f = mockFetch({ source: 'notes', enabled: false, permission: null })
    await toggleSource('apple/notes', false)
    const [url, init] = lastCall(f)
    expect(url).toBe('/api/sources/apple%2Fnotes/toggle')
    expect(init.method).toBe('POST')
    expect(JSON.parse(init.body as string)).toEqual({ enabled: false })
  })

  it('resetWatermark → PUT to the watermark/reset path', async () => {
    const f = mockFetch({})
    await resetWatermark('mail')
    const [url, init] = lastCall(f)
    expect(url).toBe('/api/sources/mail/watermark/reset')
    expect(init.method).toBe('PUT')
  })

  it('resetSourceData → POST to the per-source reset path', async () => {
    const f = mockFetch({ status: 'ok', source: 'mail', chunks_deleted: 0, message: '' })
    await resetSourceData('mail')
    const [url, init] = lastCall(f)
    expect(url).toBe('/api/sources/mail/reset')
    expect(init.method).toBe('POST')
  })

  it('recheckFda → POST /api/permissions/recheck-fda', async () => {
    const f = mockFetch({ status: 'authorized' })
    await recheckFda()
    expect(lastCall(f)[1].method).toBe('POST')
    expect(lastCall(f)[0]).toBe('/api/permissions/recheck-fda')
  })

  it('getKnowledgeSources → GET /api/knowledge/sources', async () => {
    const f = mockFetch([])
    await getKnowledgeSources()
    const [url, init] = lastCall(f)
    expect(url).toBe('/api/knowledge/sources')
    expect(init.method ?? 'GET').toBe('GET')
  })

  it('putKnowledgeSources → PUT /api/knowledge/sources with the list body', async () => {
    const f = mockFetch({ status: 'ok', count: 1 })
    await putKnowledgeSources([{ id: 'rss-1', type: 'rss', url: 'https://x/feed' }])
    const [url, init] = lastCall(f)
    expect(url).toBe('/api/knowledge/sources')
    expect(init.method).toBe('PUT')
    expect(JSON.parse(init.body as string)).toEqual([
      { id: 'rss-1', type: 'rss', url: 'https://x/feed' },
    ])
  })

  it('resolveKnowledgeSource → POST /api/knowledge/resolve with the url', async () => {
    const f = mockFetch({ type: 'rss', label: 'X', axis: 'world' })
    await resolveKnowledgeSource('https://x/feed')
    const [url, init] = lastCall(f)
    expect(url).toBe('/api/knowledge/resolve')
    expect(init.method).toBe('POST')
    expect(JSON.parse(init.body as string)).toEqual({ url: 'https://x/feed' })
  })

  it('resetWhatsAppLog → POST /api/sources/whatsapp/log/reset', async () => {
    const f = mockFetch({ status: 'ok', source: 'whatsapp', chunks_deleted: 0, message: '' })
    await resetWhatsAppLog()
    const [url, init] = lastCall(f)
    expect(url).toBe('/api/sources/whatsapp/log/reset')
    expect(init.method).toBe('POST')
  })
})

describe('knowledge client', () => {
  it('runKnowledge → POST /api/knowledge/run', async () => {
    const f = mockFetch({})
    await runKnowledge()
    const [url, init] = lastCall(f)
    expect(url).toBe('/api/knowledge/run')
    expect(init.method).toBe('POST')
  })

  it('deleteBriefing encodes the date and uses DELETE', async () => {
    const f = mockFetch({ deleted: 1, date: '2026-06-04', vault: true })
    await deleteBriefing('2026-06-04')
    const [url, init] = lastCall(f)
    expect(url).toBe('/api/briefings/2026-06-04')
    expect(init.method).toBe('DELETE')
  })

  it('getBriefing → GET /api/briefings/{date}', async () => {
    const f = mockFetch({ date: '2026-06-04', title: 't', htmlBody: '' })
    await getBriefing('2026-06-04')
    expect(lastCall(f)[0]).toBe('/api/briefings/2026-06-04')
  })

  it('listBriefings → GET /api/briefings', async () => {
    const f = mockFetch({ items: [] })
    await listBriefings()
    const [url, init] = lastCall(f)
    expect(url).toBe('/api/briefings')
    expect(init.method ?? 'GET').toBe('GET')
  })

  it('resetBriefings → POST /api/briefings/reset', async () => {
    const f = mockFetch({ status: 'ok', chunks_deleted: 0, vault_files_deleted: 0 })
    await resetBriefings()
    const [url, init] = lastCall(f)
    expect(url).toBe('/api/briefings/reset')
    expect(init.method).toBe('POST')
  })
})

describe('sources_ext client', () => {
  it('setGoogleCalendarSelected encodes id and PATCHes selected', async () => {
    const f = mockFetch({ ok: true })
    await setGoogleCalendarSelected('cal@group.calendar.google.com', true)
    const [url, init] = lastCall(f)
    expect(url).toBe('/api/google-calendar/calendars/cal%40group.calendar.google.com')
    expect(JSON.parse(init.body as string)).toEqual({ selected: true })
  })

  it('pickFolder → POST /api/pick-folder with the prompt', async () => {
    const f = mockFetch({ path: '/Users/x/Docs' })
    await pickFolder('Choose a folder')
    const [url, init] = lastCall(f)
    expect(url).toBe('/api/pick-folder')
    expect(JSON.parse(init.body as string)).toEqual({ prompt: 'Choose a folder' })
  })

  it('getWhatsAppChats → GET /api/whatsapp/chats', async () => {
    const f = mockFetch([])
    await getWhatsAppChats()
    const [url, init] = lastCall(f)
    expect(url).toBe('/api/whatsapp/chats')
    expect(init.method ?? 'GET').toBe('GET')
  })

  it('patchWhatsAppChat encodes the chat id and PATCHes the group_type', async () => {
    const f = mockFetch({ chat_id: 'a/b', group_type: 'family' })
    await patchWhatsAppChat('a/b', 'family')
    const [url, init] = lastCall(f)
    expect(url).toBe('/api/whatsapp/chats/a%2Fb')
    expect(init.method).toBe('PATCH')
    expect(JSON.parse(init.body as string)).toEqual({ group_type: 'family' })
  })

  it('resetWhatsAppPairing → POST /api/whatsapp/reset', async () => {
    const f = mockFetch({ status: 'ok' })
    await resetWhatsAppPairing()
    const [url, init] = lastCall(f)
    expect(url).toBe('/api/whatsapp/reset')
    expect(init.method).toBe('POST')
  })

  it('disconnectCalendarOAuth → DELETE /api/calendar/auth', async () => {
    const f = mockFetch({ status: 'ok' })
    await disconnectCalendarOAuth()
    const [url, init] = lastCall(f)
    expect(url).toBe('/api/calendar/auth')
    expect(init.method).toBe('DELETE')
  })

  it('getGoogleAuthUrl → GET /api/calendar/auth/url', async () => {
    const f = mockFetch({ url: 'https://accounts.google', state: 's' })
    await getGoogleAuthUrl()
    expect(lastCall(f)[0]).toBe('/api/calendar/auth/url')
  })

  it('uploadGoogleClientSecrets → POST /api/calendar/secrets/upload with the content', async () => {
    const f = mockFetch({ ok: true, path: '/p', client_type: 'installed' })
    await uploadGoogleClientSecrets('{"installed":{}}')
    const [url, init] = lastCall(f)
    expect(url).toBe('/api/calendar/secrets/upload')
    expect(init.method).toBe('POST')
    expect(JSON.parse(init.body as string)).toEqual({ content: '{"installed":{}}' })
  })

  it('getGoogleCalendars → GET /api/google-calendar/calendars', async () => {
    const f = mockFetch([])
    await getGoogleCalendars()
    expect(lastCall(f)[0]).toBe('/api/google-calendar/calendars')
  })

  it('resetGoogleCalendarSyncToken → POST /api/google-calendar/sync-token/reset', async () => {
    const f = mockFetch({ ok: true })
    await resetGoogleCalendarSyncToken()
    const [url, init] = lastCall(f)
    expect(url).toBe('/api/google-calendar/sync-token/reset')
    expect(init.method).toBe('POST')
  })

  it('setGoogleCalendarGroupType encodes id and PATCHes group_type', async () => {
    const f = mockFetch({ ok: true })
    await setGoogleCalendarGroupType('cal@group.calendar.google.com', 'work')
    const [url, init] = lastCall(f)
    expect(url).toBe('/api/google-calendar/calendars/cal%40group.calendar.google.com')
    expect(init.method).toBe('PATCH')
    expect(JSON.parse(init.body as string)).toEqual({ group_type: 'work' })
  })

  it('getWhoopStatus → GET /api/whoop/status', async () => {
    const f = mockFetch({ client: true, connected: false, redirect_uri: 'http://127.0.0.1' })
    await getWhoopStatus()
    expect(lastCall(f)[0]).toBe('/api/whoop/status')
  })

  it('uploadWhoopCredentials → POST /api/whoop/credentials/upload with id+secret', async () => {
    const f = mockFetch({ ok: true })
    await uploadWhoopCredentials('id-123', 'secret-456')
    const [url, init] = lastCall(f)
    expect(url).toBe('/api/whoop/credentials/upload')
    expect(init.method).toBe('POST')
    expect(JSON.parse(init.body as string)).toEqual({
      client_id: 'id-123',
      client_secret: 'secret-456', // pragma: allowlist secret
    })
  })

  it('openWhoopAuth → POST /api/whoop/auth/open', async () => {
    const f = mockFetch({ opened: true, url: 'https://whoop', state: 's' })
    await openWhoopAuth()
    const [url, init] = lastCall(f)
    expect(url).toBe('/api/whoop/auth/open')
    expect(init.method).toBe('POST')
  })

  it('disconnectWhoop → DELETE /api/whoop/auth', async () => {
    const f = mockFetch({ ok: true })
    await disconnectWhoop()
    const [url, init] = lastCall(f)
    expect(url).toBe('/api/whoop/auth')
    expect(init.method).toBe('DELETE')
  })

  it('whatsappQrUrl returns the QR image path with a cache-busting ts', () => {
    expect(whatsappQrUrl()).toMatch(/^\/api\/whatsapp\/qr\.png\?ts=\d+$/)
  })
})

describe('model client', () => {
  it('getModelCatalog → GET /api/model/catalog (forwards signal)', async () => {
    const f = mockFetch({ models: [], selection: {}, defaults: {} })
    const ctrl = new AbortController()
    await getModelCatalog(ctrl.signal)
    const [url, init] = lastCall(f)
    expect(url).toBe('/api/model/catalog')
    expect(init.signal).toBe(ctrl.signal)
  })

  it('deleteModel → POST /api/model/delete with the tier', async () => {
    const f = mockFetch({ tier: 'small', deleted: true })
    await deleteModel('small')
    const [url, init] = lastCall(f)
    expect(url).toBe('/api/model/delete')
    expect(init.method).toBe('POST')
    expect(JSON.parse(init.body as string)).toEqual({ tier: 'small' })
  })
})
