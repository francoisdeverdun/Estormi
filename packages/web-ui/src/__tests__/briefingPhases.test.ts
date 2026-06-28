/**
 * Tests for ``lib/briefingPhases`` — the pure parser that turns the briefing
 * engine's single log file into the agent-flow shown in the BriefingAtelier.
 * Marker lines are taken verbatim from a real knowledge.log run.
 */
import { describe, expect, it } from 'vitest'
import { parseBriefingLog } from '../lib/briefingPhases'

// A trimmed but faithful slice of a completed run (chronological, oldest first).
const FULL_RUN = `
[briefing] 22:36:50 INFO world corpus: 165 chunk(s) → 79 item(s) across 5 source(s)
[briefing] 22:37:05 INFO finary/…: 2 bullets parsed
[briefing] 22:37:37 INFO items split: 2 news, 3 other
[briefing] 22:37:38 INFO news_synthesis: 4125 chars
[briefing] 22:37:37 INFO day_vision: extractor facts — 1 physical, 0 partner, 0 open loops, tight=False
[briefing] 22:37:38 INFO enrichments: weather='overcast, 14–20°C', 0 tight transition(s), work_location=''
[briefing] 22:37:40 INFO HTTP Request: POST http://127.0.0.1:8000/search_memory "HTTP/1.1 200 OK"
[briefing] 22:37:42 INFO event correlations: 5 event(s) with related cross-source chunks
[briefing] 22:37:43 INFO correlation graph: 4 cross-source thread(s); dominant anchor='déclaration · impôts'
[briefing] 22:37:43 INFO day_vision: calling LLM (claude-cli/opus, prompt=26740 chars)
[briefing] 22:39:48 INFO day_vision: LLM returned 2228 chars
[briefing] 22:39:48 INFO vision_html: 2228 chars
[briefing] 22:39:52 INFO briefing critic: approved (no issues)
[briefing] 22:39:52 INFO vault write: briefings/2026-06-03.json
[briefing] 22:39:52 INFO Done: 8 sources, 79 new items, 6 actions
`

// The phase id/label pairs are stable across runs, so read them from a parsed
// trace (the public output) rather than reaching for an internal phase table.
const PHASE_IDS = new Map(parseBriefingLog('', false).phases.map((p) => [p.label, p.id]))
const id = (label: string) => PHASE_IDS.get(label)!

describe('parseBriefingLog — completed run', () => {
  const trace = parseBriefingLog(FULL_RUN, false)
  const byId = Object.fromEntries(trace.phases.map((p) => [p.id, p]))

  it('marks the run done and not failed', () => {
    expect(trace.done).toBe(true)
    expect(trace.failed).toBe(false)
  })

  it('marks every phase done', () => {
    expect(trace.phases.every((p) => p.status === 'done')).toBe(true)
  })

  it('extracts the correlation-graph badge + dominant anchor', () => {
    const c = byId[id('Correlation')]
    expect(c.badge).toBe('4 threads')
    expect(c.detail).toContain('déclaration · impôts')
  })

  it('reports the critic verdict', () => {
    expect(byId[id('Critic')].badge).toBe('approved')
  })

  it('counts world sources and vision size', () => {
    expect(byId[id('World sources')].badge).toBe('5 sources')
    expect(byId[id('Vision')].badge).toBe('2228 chars')
  })

  it('drops HTTP transport noise from the readable lines', () => {
    expect(trace.lines.some((l) => /HTTP Request/.test(l.message))).toBe(false)
  })

  it('flags marker lines for highlighting', () => {
    const marker = trace.lines.find((l) => /correlation graph:/.test(l.message))!
    expect(marker.isMarker).toBe(true)
    expect(marker.phaseId).toBe(id('Correlation'))
  })
})

describe('parseBriefingLog — mid-run', () => {
  const PARTIAL = `
[briefing] 22:36:50 INFO world corpus: 165 chunk(s) → 79 item(s) across 5 source(s)
[briefing] 22:37:38 INFO news_synthesis: 4125 chars
[briefing] 22:37:37 INFO day_vision: extractor facts — 1 physical, 0 partner, 0 open loops, tight=False
`
  it('marks the frontier phase active while running', () => {
    const trace = parseBriefingLog(PARTIAL, true)
    const byId = Object.fromEntries(trace.phases.map((p) => [p.id, p]))
    expect(trace.done).toBe(false)
    expect(byId[id('World sources')].status).toBe('done')
    expect(byId[id('Press synthesis')].status).toBe('done')
    expect(byId[id('Extractor')].status).toBe('active')
    expect(byId[id('Vision')].status).toBe('idle')
    expect(byId[id('Vault')].status).toBe('idle')
  })
})

describe('parseBriefingLog — failure', () => {
  it('paints the frontier phase failed on an ERROR line', () => {
    const FAILED = `
[briefing] 22:36:50 INFO world corpus: 12 chunk(s) → 3 item(s) across 2 source(s)
[briefing] 22:37:01 ERROR day_vision: extractor facts — LLM call failed
`
    const trace = parseBriefingLog(FAILED, false)
    const byId = Object.fromEntries(trace.phases.map((p) => [p.id, p]))
    expect(trace.failed).toBe(true)
    expect(byId[id('Extractor')].status).toBe('failed')
  })
})

describe('parseBriefingLog — multi-run tail', () => {
  // A tail spanning a finished run then a fresh one mid-flight. The flow must
  // reflect ONLY the current run, not inherit the earlier run's "Done:".
  const TWO_RUNS = `
[briefing] 22:39:52 INFO briefing critic: approved (no issues)
[briefing] 22:39:52 INFO vault write: briefings/2026-06-03.json
[briefing] 22:39:52 INFO Done: 8 sources, 79 new items, 6 actions
[briefing] 23:01:00 INFO Starting briefing pipeline (DB: /tmp/estormi.db)
[briefing] 23:01:05 INFO world corpus: 40 chunk(s) → 12 item(s) across 3 source(s)
[briefing] 23:01:40 INFO day_vision: extractor facts — 0 physical, 0 partner, 1 open loops, tight=False
`
  it('scopes phases to the current run, not the previous Done', () => {
    const trace = parseBriefingLog(TWO_RUNS, true)
    const byId = Object.fromEntries(trace.phases.map((p) => [p.id, p]))
    expect(trace.done).toBe(false)
    expect(byId[id('Extractor')].status).toBe('active')
    expect(byId[id('Vault')].status).toBe('idle')
    expect(byId[id('World sources')].badge).toBe('3 sources')
  })
})

describe('parseBriefingLog — empty', () => {
  it('returns all phases idle for empty input', () => {
    const trace = parseBriefingLog('', false)
    expect(trace.lines).toHaveLength(0)
    expect(trace.phases.every((p) => p.status === 'idle')).toBe(true)
  })
})
