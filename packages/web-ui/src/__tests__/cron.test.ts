/**
 * Tests for ``lib/cron`` — the 5-field cron evaluator that powers the
 * Sources panel's "next in X" hint. Only the subset the UI relies on is
 * covered; the authoritative scheduler (APScheduler in the sidecar) runs a
 * full implementation.
 */
import { describe, expect, it } from 'vitest'
import { formatRelative, nextCronTime } from '../lib/cron'

describe('nextCronTime', () => {
  it('returns null for empty or "manual"', () => {
    expect(nextCronTime('')).toBeNull()
    expect(nextCronTime('   ')).toBeNull()
    expect(nextCronTime('manual')).toBeNull()
  })

  it('returns null for malformed expressions', () => {
    expect(nextCronTime('not a cron')).toBeNull()
    expect(nextCronTime('0 0 0 0')).toBeNull() // four fields
  })

  it('"0 3 * * *" → tomorrow at 03:00 when called at 14:00', () => {
    const from = new Date('2026-05-29T14:00:00')
    const next = nextCronTime('0 3 * * *', from)!
    expect(next.getHours()).toBe(3)
    expect(next.getMinutes()).toBe(0)
    expect(next.getDate()).toBe(30) // next day
  })

  it('"0 3 * * *" → today at 03:00 when called just before', () => {
    const from = new Date('2026-05-29T02:30:00')
    const next = nextCronTime('0 3 * * *', from)!
    expect(next.getHours()).toBe(3)
    expect(next.getMinutes()).toBe(0)
    expect(next.getDate()).toBe(29)
  })

  it('"*/15 * * * *" → next 15-minute slot', () => {
    const from = new Date('2026-05-29T14:07:30')
    const next = nextCronTime('*/15 * * * *', from)!
    expect(next.getHours()).toBe(14)
    expect(next.getMinutes()).toBe(15)
  })

  it('"0 9-17 * * 1-5" honours both hour range and weekday range', () => {
    // Saturday afternoon → next firing is Monday 09:00.
    const from = new Date('2026-05-30T14:00:00') // Saturday
    const next = nextCronTime('0 9-17 * * 1-5', from)!
    expect(next.getDay()).toBe(1) // Monday
    expect(next.getHours()).toBe(9)
    expect(next.getMinutes()).toBe(0)
  })
})

describe('formatRelative', () => {
  const now = new Date('2026-05-29T08:00:00')

  it('returns — when target is null or in the past', () => {
    expect(formatRelative(null, now)).toBe('—')
    expect(formatRelative(new Date('2026-05-29T07:00:00'), now)).toBe('—')
  })

  it('formats sub-minute', () => {
    expect(formatRelative(new Date('2026-05-29T08:00:30'), now)).toBe('< 1m')
  })

  it('formats minutes only', () => {
    expect(formatRelative(new Date('2026-05-29T08:42:00'), now)).toBe('42m')
  })

  it('formats hours + minutes', () => {
    expect(formatRelative(new Date('2026-05-29T11:15:00'), now)).toBe('3h 15m')
  })

  it('formats days + hours', () => {
    expect(formatRelative(new Date('2026-05-30T20:00:00'), now)).toBe('1d 12h')
  })
})
