/**
 * Historic-depth helpers for SourceManageModal — option sets, per-source
 * defaults, normalisation and the human hint. Extracted so the modal body
 * stays focused on layout.
 */

// Most depth-capable sources offer month/year windows. News-style sources
// (knowledge) want week/month granularity instead — a first run shouldn't
// pull months of feeds. The active option set is chosen per source.
const HISTORIC_OPTIONS = ['90D', '6M', '1Y', '2Y', 'ALL'] as const
const HISTORIC_OPTIONS_KNOWLEDGE = ['1W', '2W', '1M', '3M', 'ALL'] as const
export type HistoricOption = string

export function depthOptions(key: string): readonly string[] {
  return key === 'knowledge' ? HISTORIC_OPTIONS_KNOWLEDGE : HISTORIC_OPTIONS
}

// First-run default per source — mirrors each connector spec's `default_depth`
// (knowledge → 1w, whatsapp → 2y) and the universal 90d fallback for the rest.
function depthDefault(key: string): HistoricOption {
  if (key === 'knowledge') return '1W'
  if (key === 'whatsapp') return '2Y'
  return '90D'
}

// Sources whose ingest honours a first-run history window. The others
// (reminders, documents) have no time-window concept, so the depth picker is
// hidden for them entirely. WhatsApp pages older history on demand back to this
// horizon when the bridge (re-)pairs. Mirrors the `_DEPTH_ENV` map derived from
// the connector specs in estormi_server/server/launchers/ingestion.py.
export const DEPTH_SOURCES = new Set([
  'notes',
  'mail',
  'gcal',
  'imessage',
  'knowledge',
  'whatsapp',
])

export function normaliseHistoric(
  key: string | undefined,
  raw: string | undefined,
): HistoricOption {
  const v = (raw ?? '').toUpperCase()
  if (depthOptions(key ?? '').includes(v)) return v
  // Per-source default — mirrors each spec's `default_depth` (knowledge → 1W)
  // and the universal 90D fallback.
  return depthDefault(key ?? '')
}

export function historicHint(key: string, h: HistoricOption): string {
  if (h === 'ALL') return 'Ingest the full history from the source.'
  if (key === 'knowledge')
    return `The first ingestion reaches back the last ${h.toLowerCase()}; later runs only fetch what's new.`
  if (key === 'gcal')
    return `Only events within the last ${h.toLowerCase()} will be ingested.`
  if (key === 'mail')
    return `Only messages received within the last ${h.toLowerCase()} are pulled.`
  if (key === 'whatsapp')
    return `On (re-)pairing, history is pulled back the last ${h.toLowerCase()}; later syncs only fetch new messages.`
  return `Limit ingestion window to the last ${h.toLowerCase()}.`
}
