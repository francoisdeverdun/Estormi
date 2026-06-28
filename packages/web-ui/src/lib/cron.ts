/**
 * Tiny 5-field cron evaluator for the Sources panel header.
 *
 * Supports the common subset: ``*``, single integers, comma lists, ranges
 * (``1-5``), and steps (``* /N``). The Mac sidecar's APScheduler is the
 * authoritative scheduler — this helper just lets the UI display a friendly
 * "next in <human>" hint without a round-trip.
 *
 * Returns the next firing time strictly after ``from`` (no seconds-precision
 * — APScheduler fires on the minute), or ``null`` when:
 *   - the expression is malformed, or
 *   - it never fires within the four-year search horizon (a footgun the user
 *     should see as a question mark rather than a stale promise).
 */

function parseField(raw: string, min: number, max: number): Set<number> | null {
  // `*` → every value in range. We return `null` to signal "no constraint"
  // so the matcher skips the Set lookup — both faster and trivially correct.
  if (raw === '*') return null
  const out = new Set<number>()
  for (const part of raw.split(',')) {
    if (part.includes('/')) {
      const [rangeRaw, stepRaw] = part.split('/')
      const step = parseInt(stepRaw, 10)
      if (!Number.isFinite(step) || step <= 0) throw new Error('bad step')
      let lo = min
      let hi = max
      if (rangeRaw && rangeRaw !== '*') {
        if (rangeRaw.includes('-')) {
          const [a, b] = rangeRaw.split('-').map((s) => parseInt(s, 10))
          lo = a
          hi = b
        } else {
          lo = parseInt(rangeRaw, 10)
          hi = max
        }
      }
      if (!Number.isFinite(lo) || !Number.isFinite(hi)) throw new Error('bad range')
      for (let v = lo; v <= hi; v += step) out.add(v)
    } else if (part.includes('-')) {
      const [a, b] = part.split('-').map((s) => parseInt(s, 10))
      if (!Number.isFinite(a) || !Number.isFinite(b)) throw new Error('bad range')
      for (let v = a; v <= b; v++) out.add(v)
    } else {
      const n = parseInt(part, 10)
      if (!Number.isFinite(n)) throw new Error('bad number')
      out.add(n)
    }
  }
  return out
}

export function nextCronTime(cron: string, from: Date = new Date()): Date | null {
  const trimmed = (cron || '').trim()
  if (!trimmed || trimmed === 'manual') return null
  const fields = trimmed.split(/\s+/)
  if (fields.length !== 5) return null

  let minutes: Set<number> | null
  let hours: Set<number> | null
  let dom: Set<number> | null
  let mon: Set<number> | null
  let dow: Set<number> | null
  try {
    minutes = parseField(fields[0], 0, 59)
    hours = parseField(fields[1], 0, 23)
    dom = parseField(fields[2], 1, 31)
    mon = parseField(fields[3], 1, 12)
    dow = parseField(fields[4], 0, 6)
    // Cron/APScheduler treat dow 7 as Sunday (== 0), but getDay() only returns
    // 0..6 so a 7 in the set could never match. Normalise it (covers the literal
    // `7` and any range/list like `5-7` that yields 7).
    if (dow && dow.has(7)) {
      dow.delete(7)
      dow.add(0)
    }
  } catch {
    return null
  }

  const cur = new Date(from)
  cur.setSeconds(0, 0)
  // Start at the next whole minute so we never return ``from`` itself.
  cur.setMinutes(cur.getMinutes() + 1)

  // Search up to ~4 years forward. A correctly-formed cron always lands
  // within a year, so this is a generous safety horizon for the pathological
  // "every Feb 29 at 02:00" case.
  const horizonMs = 4 * 366 * 24 * 60 * 60 * 1000
  const deadline = cur.getTime() + horizonMs
  while (cur.getTime() <= deadline) {
    const m = cur.getMinutes()
    const h = cur.getHours()
    const d = cur.getDate()
    const mo = cur.getMonth() + 1
    const dw = cur.getDay() // Sunday = 0, matching cron + APScheduler convention.
    // Vixie-cron day rule: when BOTH day-of-month and day-of-week are
    // restricted (neither is `*`), the day matches if EITHER matches — not
    // both. With one side `*`, fall back to the plain AND.
    // Inline the null checks (rather than via boolean consts) so TS narrows
    // dom/dow to non-null inside the EITHER branch.
    const dayMatch =
      dom !== null && dow !== null
        ? dom.has(d) || dow.has(dw)
        : (dom === null || dom.has(d)) && (dow === null || dow.has(dw))
    if (
      (minutes === null || minutes.has(m)) &&
      (hours === null || hours.has(h)) &&
      (mon === null || mon.has(mo)) &&
      dayMatch
    ) {
      return cur
    }
    cur.setMinutes(cur.getMinutes() + 1)
  }
  return null
}

/** Format a future ``Date`` as ``"in 3h 12m"`` (or ``"< 1m"`` / ``"—"``). */
export function formatRelative(target: Date | null, from: Date = new Date()): string {
  if (!target) return '—'
  const ms = target.getTime() - from.getTime()
  if (ms < 0) return '—'
  const secs = Math.floor(ms / 1000)
  const days = Math.floor(secs / 86400)
  const hours = Math.floor((secs % 86400) / 3600)
  const mins = Math.floor((secs % 3600) / 60)
  if (days >= 1) return hours ? `${days}d ${hours}h` : `${days}d`
  if (hours >= 1) return mins ? `${hours}h ${mins}m` : `${hours}h`
  if (mins >= 1) return `${mins}m`
  return '< 1m'
}
