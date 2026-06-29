/**
 * Base HTTP client for the SPA.
 *
 * In dev, Vite proxies /api → :8000. In prod, same origin (FastAPI serves
 * the SPA from /app/ next to /api/).
 *
 * Every state-changing request (POST/PUT/PATCH/DELETE) sends the
 * ``X-Estormi-Origin: tauri`` header. The FastAPI CSRF gate
 * (``estormi_server/server/security.py``) rejects same-origin POSTs without
 * this stamp — every browser request the SPA makes is considered a
 * first-party operation and needs to carry it. GET requests are exempt
 * (they're idempotent + read-only).
 */
const BASE = ''
const CSRF_HEADER = 'X-Estormi-Origin'
const CSRF_VALUE = 'tauri'

export class ApiError extends Error {
  status: number
  constructor(status: number, message: string) {
    super(message)
    this.status = status
  }
}

export async function apiGet<T>(path: string, signal?: AbortSignal): Promise<T> {
  const r = await fetch(`${BASE}${path}`, {
    headers: { Accept: 'application/json' },
    // `no-store` is load-bearing: the SPA polls read endpoints (overview every
    // 5s, etc.), and WKWebView will otherwise heuristically cache a GET with no
    // Cache-Control and serve the same stale body forever — e.g. an overview
    // snapshot from a moment when a source toggle or pairing state differed,
    // freezing the row chips until a hard reload. Always fetch fresh.
    cache: 'no-store',
    signal,
  })
  if (!r.ok) throw new ApiError(r.status, `GET ${path} → ${r.status}`)
  return (await r.json()) as T
}

/**
 * Liveness probe for the FastAPI sidecar — resolves ``true`` once ``/health``
 * answers 2xx, ``false`` while it is still unreachable. Kept in this module so
 * the SPA keeps a single ``fetch`` chokepoint (origin/CSRF discipline). The
 * probe is GET + read-only, so it carries no CSRF stamp, and it swallows
 * connection errors because the caller polls it in a boot loop.
 */
export async function pingHealth(signal?: AbortSignal): Promise<boolean> {
  try {
    const r = await fetch(`${BASE}/health`, { cache: 'no-store', signal })
    return r.ok
  } catch {
    return false
  }
}

/**
 * The return type is ``T | null`` because some endpoints respond with an
 * empty body (e.g. ``204 No Content``). The previous signature returned
 * ``Promise<T>`` and cast empty bodies to ``null as T``, which lied to
 * every caller: code like ``const r = await apiSend<{ok: boolean}>(...)``
 * would crash on ``r.ok`` when the server happened to return no body.
 * Callers must now narrow ``null`` explicitly.
 */
export async function apiSend<T>(
  path: string,
  method: 'POST' | 'PUT' | 'DELETE' | 'PATCH',
  body?: unknown,
): Promise<T | null> {
  const r = await fetch(`${BASE}${path}`, {
    method,
    headers: {
      'Content-Type': 'application/json',
      Accept: 'application/json',
      [CSRF_HEADER]: CSRF_VALUE,
    },
    body: body === undefined ? undefined : JSON.stringify(body),
  })
  if (!r.ok) {
    // Surface the server's ``{error: …}`` body when present (the validation
    // endpoints return one) so callers can show the actual reason instead of a
    // bare status code; fall back to the generic line otherwise.
    let detail = ''
    try {
      const parsed = JSON.parse(await r.text())
      if (parsed && typeof parsed.error === 'string') detail = parsed.error
    } catch {
      /* non-JSON / empty error body — keep the generic message */
    }
    throw new ApiError(r.status, detail || `${method} ${path} → ${r.status}`)
  }
  const text = await r.text()
  if (!text) return null
  try {
    return JSON.parse(text) as T
  } catch {
    throw new ApiError(r.status, 'invalid JSON response')
  }
}
