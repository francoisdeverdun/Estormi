/**
 * Tests for ``lib/watermark`` — the ISO-timestamp compactor behind the
 * SourceRow watermark column. Only ISO-shaped inputs are touched; everything
 * else passes through unchanged so non-timestamp watermarks ("sync tokens")
 * still read.
 */
import { describe, expect, it } from 'vitest'
import { compactWatermark } from '../lib/watermark'

describe('compactWatermark', () => {
  it('passes through empty and em-dash placeholders', () => {
    expect(compactWatermark('')).toBe('')
    expect(compactWatermark('—')).toBe('—')
  })

  it('compacts a full ISO timestamp to MM-DD HH:MM', () => {
    expect(compactWatermark('2026-05-15T09:30:12Z')).toBe('05-15 09:30')
    expect(compactWatermark('2026-05-15 09:30')).toBe('05-15 09:30')
  })

  it('compacts a date-only ISO string to MM-DD', () => {
    expect(compactWatermark('2026-05-15')).toBe('05-15')
  })

  it('leaves non-ISO watermarks unchanged', () => {
    expect(compactWatermark('sync tokens')).toBe('sync tokens')
    expect(compactWatermark('live staging')).toBe('live staging')
  })
})
