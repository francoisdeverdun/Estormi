/**
 * Storage-location client — the single root path the whole library lives under.
 *
 * ``/api/storage/location`` reports the current data dir, its free space + size,
 * and any queued relocation. ``/api/storage/relocate`` only *queues* a move
 * (the server writes a marker); the copy → verify → swap happens at the next app
 * start, so the UI tells the user to reopen. See ``estormi_server/api/storage.py``
 * and ``memory_core/datadir.py``.
 */
import { apiGet, apiSend } from './client'

export interface StorageLocation {
  /** The current library directory. */
  dir: string
  /** The default location (config home) — shown as a hint / reset target. */
  default: string
  /** Free GB on the current volume (null if unprobeable). */
  freeGb: number | null
  /** Total size of the current library on disk. */
  libraryBytes: number
  /** Destination of a queued move (takes effect on reopen), or null. */
  pending: string | null
}

export interface RelocateResult {
  ok: boolean
  willMoveOnRestart: boolean
  from: string
  to: string
  bytes: number
}

export const getStorageLocation = (signal?: AbortSignal) =>
  apiGet<StorageLocation>('/api/storage/location', signal)

export const relocateStorage = (to: string) =>
  apiSend<RelocateResult>('/api/storage/relocate', 'POST', { to })
