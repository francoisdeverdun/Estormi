/**
 * compactWatermark — compact an ISO timestamp watermark to ``MM-DD HH:MM`` so
 * the SourceRow watermark column lines up across rows. Non-ISO watermarks
 * (``"sync tokens"``, ``"live staging"``, raw fallback strings) are returned
 * unchanged — only ISO-shaped inputs are touched. The caller preserves the full
 * timestamp in the cell's ``title`` so the raw form stays available on hover.
 *
 * Pure string formatter, extracted from SourceRow.tsx so it can be unit-tested
 * (mirrors the other date helpers under `lib/`).
 */
export function compactWatermark(raw: string): string {
  if (!raw || raw === '—') return raw
  // YYYY-MM-DD or YYYY-MM-DDTHH:MM(:SS)?(Z|+offset)? — both forms accepted.
  const m = /^(\d{4})-(\d{2})-(\d{2})(?:[T\s](\d{2}):(\d{2}))?/.exec(raw)
  if (!m) return raw
  const [, , mo, d, hh, mm] = m
  return hh && mm ? `${mo}-${d} ${hh}:${mm}` : `${mo}-${d}`
}
