/**
 * Tests for ``lib/logFormat`` — the shared log tokeniser/parser the briefing
 * Atelier and the ingestion log panes render through. Lines are taken from real
 * knowledge.log / DAG / per-source logs.
 */
import { describe, expect, it } from 'vitest'
import {
  DAG_RUN_START,
  SOURCE_MARKER,
  SOURCE_RUN_START,
  parseLogLines,
  scopeToLatestRun,
} from '../lib/logFormat'

describe('parseLogLines — tokenising', () => {
  it('parses an ingestion tag line', () => {
    const [l] = parseLogLines('[21:50:16] [connectors] notes: starting')
    expect(l).toMatchObject({ time: '21:50:16', tag: 'connectors', message: 'notes: starting', isRunBreak: false })
  })

  it('parses an ingestion line with no tag', () => {
    const [l] = parseLogLines('[22:57:49] 0 notes exported to /tmp/notes/')
    expect(l).toMatchObject({ time: '22:57:49', tag: '', message: '0 notes exported to /tmp/notes/' })
  })

  it('parses a briefing line, keeping the level token as the tag', () => {
    const [l] = parseLogLines('[briefing] 22:37:43 INFO correlation graph: 4 cross-source thread(s)')
    expect(l).toMatchObject({ time: '22:37:43', tag: 'INFO', level: 'INFO' })
    expect(l.message).toContain('correlation graph')
  })

  it('parses a distill line the same way as a briefing line', () => {
    const [l] = parseLogLines("[distill] 20:47:24 INFO dataset: {'train': 95, 'valid': 17}")
    expect(l).toMatchObject({ time: '20:47:24', tag: 'INFO', level: 'INFO' })
    expect(l.message).toContain('dataset:')
  })

  it('drops httpx transport-noise lines', () => {
    const lines = parseLogLines(
      '[distill] 20:47:24 INFO HTTP Request: POST http://127.0.0.1:8000/fetch_around "HTTP/1.1 200 OK"\n' +
        '[distill] 20:47:24 INFO archive harvest: 19 briefing(s) mirrored into refs',
    )
    expect(lines).toHaveLength(1)
    expect(lines[0].message).toContain('archive harvest')
  })

  it('renders a run-break line as a separator', () => {
    const [l] = parseLogLines('── run 20260603-215016 ──')
    expect(l.isRunBreak).toBe(true)
    expect(l.message).toBe('run 20260603-215016')
  })

  it('infers level from content for tag lines', () => {
    expect(parseLogLines('[22:57:54] [connectors] notes: ok in 5.2s')[0].level).toBe('OK')
    expect(parseLogLines('[22:57:54] [mail] mail: failed — boom')[0].level).toBe('ERROR')
    expect(parseLogLines('[22:57:54] [notes] 3 failed to post')[0].level).toBe('ERROR')
  })

  it('does not flag a zero-failure success summary as an error', () => {
    const [l] = parseLogLines('[22:57:54] [notes] Done — 0 notes processed, 0 chunks indexed (0 failed).')
    expect(l.level).toBe('OK')
  })

  it('drops empty and dropRe lines', () => {
    const raw = '[10:00:00] [briefing] keep me\n\n[10:00:01] HTTP Request: GET /x'
    const lines = parseLogLines(raw, { dropRe: /HTTP Request:/ })
    expect(lines).toHaveLength(1)
    expect(lines[0].message).toBe('keep me')
  })

  it('flags marker lines', () => {
    const [l] = parseLogLines('[22:27:55] [dag] === knowledge ok (85s) ===', { markerRe: SOURCE_MARKER })
    expect(l.isMarker).toBe(true)
  })
})

describe('parseLogLines — run scoping', () => {
  const TWO_DAG_RUNS = `
[22:00:00] [dag] starting daily ingestion DAG at 2026-06-03T22:00:00+0200
[22:00:05] [dag] === notes ok (5s) ===
[22:57:48] [dag] starting daily ingestion DAG at 2026-06-03T22:57:48+0200
[22:57:54] [dag] === notes ok (6s) ===
`
  it('keeps only the latest DAG run', () => {
    const lines = parseLogLines(TWO_DAG_RUNS, { runStartRe: DAG_RUN_START })
    expect(lines.every((l) => !l.message.includes('22:00:00') && !l.time.startsWith('22:00'))).toBe(true)
    expect(lines[0].message).toContain('22:57:48')
    expect(lines).toHaveLength(2)
  })

  it('keeps only the latest per-source run', () => {
    const SRC = `── run 20260603-100000 ──
[10:00:00] [notes] old run
── run 20260603-225748 ──
[22:57:54] [notes] current run`
    const lines = parseLogLines(SRC, { runStartRe: SOURCE_RUN_START })
    expect(lines.find((l) => l.message === 'old run')).toBeUndefined()
    expect(lines.find((l) => l.message === 'current run')).toBeDefined()
  })

  it('scopeToLatestRun returns all lines when no marker present', () => {
    const ls = ['a', 'b', 'c']
    expect(scopeToLatestRun(ls, DAG_RUN_START)).toEqual(ls)
  })
})
