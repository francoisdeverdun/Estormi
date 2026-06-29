/**
 * Tests for the TTS (voice) catalog client (``api/tts.ts``) — the only api/
 * module that previously had no test. Asserts each wrapper hits the right URL
 * with the right verb, threads its body, and forwards the AbortSignal. ``fetch``
 * is mocked per-test and we read back the call args.
 */
import { afterEach, describe, expect, it, vi } from 'vitest'
import { deleteTtsModel, getTtsCatalog } from '../api/tts'

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

function lastCall(fetchMock: ReturnType<typeof vi.fn>): [string, RequestInit] {
  const [url, init] = fetchMock.mock.calls[fetchMock.mock.calls.length - 1]
  return [url as string, (init ?? {}) as RequestInit]
}

afterEach(() => {
  vi.restoreAllMocks()
})

describe('tts client', () => {
  it('getTtsCatalog → GET /api/tts/catalog and returns the parsed catalog', async () => {
    const catalog = {
      models: [
        {
          key: 'voxtral',
          label: 'Voxtral',
          family: 'Mistral',
          min_ram_gb: 8,
          expected_bytes: 1000,
          downloaded: true,
          size_bytes: 1000,
        },
      ],
      selected: 'voxtral',
      voices: ['fr_female'],
    }
    const f = mockFetch(catalog)
    const out = await getTtsCatalog()
    expect(out).toEqual(catalog)
    const [url, init] = lastCall(f)
    expect(url).toBe('/api/tts/catalog')
    expect(init.method ?? 'GET').toBe('GET')
  })

  it('getTtsCatalog forwards the AbortSignal', async () => {
    const f = mockFetch({ models: [], selected: '', voices: [] })
    const controller = new AbortController()
    await getTtsCatalog(controller.signal)
    expect(lastCall(f)[1].signal).toBe(controller.signal)
  })

  it('deleteTtsModel → POST /api/tts/delete with the key in the body', async () => {
    const f = mockFetch({ key: 'voxtral', deleted: true })
    const out = await deleteTtsModel('voxtral')
    expect(out).toEqual({ key: 'voxtral', deleted: true })
    const [url, init] = lastCall(f)
    expect(url).toBe('/api/tts/delete')
    expect(init.method).toBe('POST')
    expect(JSON.parse(init.body as string)).toEqual({ key: 'voxtral' })
  })
})
