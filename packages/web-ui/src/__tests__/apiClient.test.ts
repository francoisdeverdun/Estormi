/**
 * Tests for the base HTTP client (``api/client.ts``).
 *
 * These guard the single fetch chokepoint that every typed client funnels
 * through: GET stays read-only (no CSRF stamp), state-changing verbs carry
 * the ``X-Estormi-Origin: tauri`` header, JSON is parsed, empty bodies map
 * to ``null``, and non-2xx responses raise a typed {@link ApiError} that
 * preserves the status code. ``global.fetch`` is mocked per-test.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { ApiError, apiGet, apiSend, pingHealth } from '../api/client'

/** Build a minimal Response-like stub the client understands. */
function res(opts: {
  ok?: boolean
  status?: number
  json?: unknown
  text?: string
}): Response {
  const { ok = true, status = 200, json, text } = opts
  return {
    ok,
    status,
    json: async () => json,
    text: async () => (text !== undefined ? text : json === undefined ? '' : JSON.stringify(json)),
  } as unknown as Response
}

describe('apiGet', () => {
  beforeEach(() => {
    vi.restoreAllMocks()
  })
  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('issues a GET with an Accept header and no CSRF stamp', async () => {
    const fetchMock = vi.fn().mockResolvedValue(res({ json: { hello: 'world' } }))
    vi.stubGlobal('fetch', fetchMock)

    const out = await apiGet<{ hello: string }>('/api/settings')

    expect(out).toEqual({ hello: 'world' })
    expect(fetchMock).toHaveBeenCalledTimes(1)
    const [url, init] = fetchMock.mock.calls[0]
    expect(url).toBe('/api/settings')
    expect((init as RequestInit).method ?? 'GET').toBe('GET')
    expect((init as RequestInit).headers).toMatchObject({ Accept: 'application/json' })
    expect((init as RequestInit).headers).not.toHaveProperty('X-Estormi-Origin')
  })

  it('forwards an AbortSignal', async () => {
    const fetchMock = vi.fn().mockResolvedValue(res({ json: {} }))
    vi.stubGlobal('fetch', fetchMock)
    const ctrl = new AbortController()
    await apiGet('/api/x', ctrl.signal)
    expect((fetchMock.mock.calls[0][1] as RequestInit).signal).toBe(ctrl.signal)
  })

  it('throws an ApiError carrying the status on a non-OK response', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(res({ ok: false, status: 503 })))
    await expect(apiGet('/api/down')).rejects.toBeInstanceOf(ApiError)
    await expect(apiGet('/api/down')).rejects.toMatchObject({ status: 503 })
  })
})

describe('pingHealth', () => {
  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('resolves true when /health answers 2xx', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(res({ ok: true })))
    await expect(pingHealth()).resolves.toBe(true)
  })

  it('resolves false when /health is unreachable (fetch rejects)', async () => {
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new Error('ECONNREFUSED')))
    await expect(pingHealth()).resolves.toBe(false)
  })

  it('uses cache: no-store so the probe never reads a stale 200', async () => {
    const fetchMock = vi.fn().mockResolvedValue(res({ ok: true }))
    vi.stubGlobal('fetch', fetchMock)
    await pingHealth()
    expect((fetchMock.mock.calls[0][1] as RequestInit).cache).toBe('no-store')
  })
})

describe('apiSend', () => {
  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('sends the X-Estormi-Origin CSRF stamp + JSON body on a POST', async () => {
    const fetchMock = vi.fn().mockResolvedValue(res({ json: { ok: true } }))
    vi.stubGlobal('fetch', fetchMock)

    const out = await apiSend<{ ok: boolean }>('/api/pipeline/run', 'POST', { stage: 'notes' })

    expect(out).toEqual({ ok: true })
    const [url, init] = fetchMock.mock.calls[0]
    expect(url).toBe('/api/pipeline/run')
    expect((init as RequestInit).method).toBe('POST')
    expect((init as RequestInit).headers).toMatchObject({
      'Content-Type': 'application/json',
      Accept: 'application/json',
      'X-Estormi-Origin': 'tauri',
    })
    expect((init as RequestInit).body).toBe(JSON.stringify({ stage: 'notes' }))
  })

  it('omits the body entirely when none is given', async () => {
    const fetchMock = vi.fn().mockResolvedValue(res({ text: '' }))
    vi.stubGlobal('fetch', fetchMock)
    await apiSend('/api/pipeline/stop', 'POST')
    expect((fetchMock.mock.calls[0][1] as RequestInit).body).toBeUndefined()
  })

  it('returns null on an empty (204-style) body', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(res({ ok: true, status: 204, text: '' })))
    await expect(apiSend('/api/x', 'DELETE')).resolves.toBeNull()
  })

  it('parses a JSON body when present', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(res({ text: JSON.stringify({ count: 3 }) })),
    )
    await expect(apiSend<{ count: number }>('/api/x', 'PUT', {})).resolves.toEqual({ count: 3 })
  })

  it('raises ApiError on a non-OK response', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(res({ ok: false, status: 403 })))
    await expect(apiSend('/api/x', 'POST')).rejects.toMatchObject({ status: 403 })
  })

  it('raises ApiError when the body is present but not valid JSON', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(res({ ok: true, text: 'not json' })))
    await expect(apiSend('/api/x', 'POST')).rejects.toMatchObject({
      message: 'invalid JSON response',
    })
  })
})
