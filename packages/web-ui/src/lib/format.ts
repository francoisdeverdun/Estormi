/**
 * Shared display formatters for the SPA — one home for the small number/byte
 * helpers that were previously copy-pasted across cards.
 */

/** Integer with thousands separators; em-dash for null/undefined/non-finite. */
export function fmtInt(v: number | null | undefined): string {
  if (v === null || v === undefined || !Number.isFinite(v)) return '—'
  return new Intl.NumberFormat('en-US').format(v)
}

/**
 * Adaptive byte size (B/KB/MB/GB) — the TS counterpart of
 * ``services/overview.py`` ``fmt_bytes``. Em-dash for null/non-finite/≤0.
 */
export function fmtBytes(n: number | null | undefined): string {
  if (n == null || !Number.isFinite(n) || n <= 0) return '—'
  const units = ['B', 'KB', 'MB', 'GB']
  let i = 0
  let v = n
  while (v >= 1024 && i < units.length - 1) {
    v /= 1024
    i++
  }
  return `${v.toFixed(v < 10 ? 1 : 0)} ${units[i]}`
}
