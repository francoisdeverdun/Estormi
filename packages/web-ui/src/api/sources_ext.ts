/**
 * Source pairing & per-source side-channels — used by the rich Sources &
 * Parameters Manage modal on the Ingestion page.
 *
 * Endpoints are owned by several backend modules:
 *
 * - ``GET    /api/whatsapp/chats``                      list paired chats        (whatsapp_settings.py)
 * - ``PATCH  /api/whatsapp/chats/{chat_id}``            tag a chat               (whatsapp_settings.py)
 * - ``POST   /api/whatsapp/reset``                      clear pairing            (whatsapp_settings.py)
 * - ``DELETE /api/calendar/auth``                       disconnect OAuth         (calendar_oauth.py)
 * - ``POST   /api/pick-folder``                         native folder picker     (apple_folder_picker.py)
 *
 * The QR for WhatsApp pairing is rendered straight from the image
 * endpoint ``GET /api/whatsapp/qr.png`` — no JSON wrapper needed.
 *
 * WhatsApp chat auto-tagging and the macOS Contacts → name index refresh
 * run automatically as part of the nightly WhatsApp ingestion stage —
 * the SPA no longer exposes manual triggers for either.
 */
import { apiGet, apiSend } from './client'
import type { components } from './schema'

/** Generated OpenAPI request-body schemas — typing the ``apiSend`` bodies
 *  against these wires the server contract into this client so a drift fails
 *  ``tsc`` rather than only at runtime. See the note in ``api/settings.ts``. */
type Schemas = components['schemas']

export interface WhatsAppChat {
  chat_id: string
  chat_name: string
  /** Semantic life-context tag (work/family/…), user- or auto-assigned. */
  group_type: string
  /** Structural kind (dm/group/broadcast), derived from the JID. Read-only. */
  chat_kind?: string | null
}

export const getWhatsAppChats = () =>
  apiGet<WhatsAppChat[]>('/api/whatsapp/chats')

export const patchWhatsAppChat = (chatId: string, groupType: string) =>
  apiSend<{ chat_id: string; group_type: string }>(
    `/api/whatsapp/chats/${encodeURIComponent(chatId)}`,
    'PATCH',
    { group_type: groupType } satisfies Schemas['_WhatsAppChatTypeBody'],
  )

export const resetWhatsAppPairing = () =>
  apiSend<{ status: string }>('/api/whatsapp/reset', 'POST')

export const disconnectCalendarOAuth = () =>
  apiSend<{ status: string }>('/api/calendar/auth', 'DELETE')

// ── Google Calendar OAuth & calendar picker ─────────────────────────────────
//
// Three states the Google Calendar panel surfaces:
//   1. server returns 400 from /api/calendar/auth/url → `google_client_secrets.json`
//      is missing. Show setup instructions.
//   2. server returns 200 with {url, state} → not connected yet. Show
//      "Connect with Google" button that opens the URL in the system browser.
//   3. /api/google-calendar/calendars returns 200 → connected. List Google
//      calendars with per-row toggles.

export interface GCalAuthUrl {
  url: string
  state: string
}

export interface GCalCalendar {
  id: string
  name: string
  color?: string
  selected: boolean
  /** User-tagged life context for events ingested from this calendar.
   *  Matches the Apple Calendar vocabulary so downstream filters work
   *  identically across both providers. Defaults to ``unknown`` until
   *  the user picks a tag. */
  group_type: GCalGroupType
}

/** Categories the user can assign to a calendar. Mirrors
 *  `GCAL_GROUP_TYPES` in estormi_server/services/calendar_oauth.py. */
export type GCalGroupType =
  | 'me'
  | 'partner'
  | 'work'
  | 'family'
  | 'couple'
  | 'friends'
  | 'organisation'
  | 'charity'
  | 'sport'
  | 'noise'
  | 'unknown'

export const GCAL_GROUP_TYPES: readonly GCalGroupType[] = [
  'unknown',
  'me',
  'partner',
  'work',
  'family',
  'couple',
  'friends',
  'organisation',
  'charity',
  'sport',
  // 'noise' mutes a calendar — kept out of the briefing's context/schedule sets,
  // matching the server vocabulary (calendar_oauth.GCAL_GROUP_TYPES).
  'noise',
] as const

export const getGoogleAuthUrl = () =>
  apiGet<GCalAuthUrl>('/api/calendar/auth/url')

/** Upload the OAuth client JSON the user downloaded from Google Cloud
 *  Console. Server validates the shape and writes it to the data dir. */
export const uploadGoogleClientSecrets = (content: string) =>
  apiSend<{ ok: boolean; path: string; client_type: string }>(
    '/api/calendar/secrets/upload',
    'POST',
    { content },
  )

export const getGoogleCalendars = () =>
  apiGet<GCalCalendar[]>('/api/google-calendar/calendars')

/** Drop the stored per-calendar sync tokens so the next gcal run does a
 *  full re-pull. OAuth is left connected; re-ingest is idempotent. */
export const resetGoogleCalendarSyncToken = () =>
  apiSend<{ ok: boolean }>('/api/google-calendar/sync-token/reset', 'POST')

export const setGoogleCalendarSelected = (id: string, selected: boolean) =>
  apiSend<{ ok: boolean }>(
    `/api/google-calendar/calendars/${encodeURIComponent(id)}`,
    'PATCH',
    { selected },
  )

/** Persist the user's life-context tag for one Google calendar. The
 *  same endpoint accepts ``selected`` and ``group_type`` together but
 *  the SPA fires them separately so each chip click can complete
 *  independently. */
export const setGoogleCalendarGroupType = (id: string, group_type: GCalGroupType) =>
  apiSend<{ ok: boolean }>(
    `/api/google-calendar/calendars/${encodeURIComponent(id)}`,
    'PATCH',
    { group_type },
  )

// ── WHOOP OAuth ─────────────────────────────────────────────────────────────
//
// Three states the WHOOP panel surfaces, all from a single /api/whoop/status
// probe (WHOOP has no "list calendars" equivalent):
//   1. { client: false }                → credentials not saved yet. Show the
//      client_id / client_secret form.
//   2. { client: true, connected: false } → not authorised yet. Show
//      "Connect with WHOOP", which opens the consent screen in the browser.
//   3. { client: true, connected: true }  → token stored and refreshable.

export interface WhoopStatus {
  /** Whether client_id / client_secret have been saved. */
  client: boolean
  /** Whether a stored OAuth token is present and still refreshable. */
  connected: boolean
  /** The loopback redirect URI the user must whitelist in their WHOOP app. */
  redirect_uri: string
}

export const getWhoopStatus = () => apiGet<WhoopStatus>('/api/whoop/status')

/** Save the WHOOP app's client_id / client_secret from developer.whoop.com. */
export const uploadWhoopCredentials = (clientId: string, clientSecret: string) =>
  apiSend<{ ok: boolean }>('/api/whoop/credentials/upload', 'POST', {
    client_id: clientId,
    client_secret: clientSecret,
  })

/** Build the consent URL and open it in the system browser. */
export const openWhoopAuth = () =>
  apiSend<{ opened: boolean; url: string; state: string }>('/api/whoop/auth/open', 'POST')

/** Delete the stored WHOOP token (Disconnect). */
export const disconnectWhoop = () => apiSend<{ ok: boolean }>('/api/whoop/auth', 'DELETE')

export const pickFolder = (prompt?: string) =>
  apiSend<{ path: string | null }>('/api/pick-folder', 'POST', { prompt })

/** Path to the WhatsApp pairing QR PNG. Image element src; no JSON. */
export const whatsappQrUrl = (): string => `/api/whatsapp/qr.png?ts=${Date.now()}`
