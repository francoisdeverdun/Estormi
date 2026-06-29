import { describe, expect, it } from 'vitest'
import { ENGINES } from '../state/SystemStatus'

// The server (estormi_server/server/jobs.py: ENGINES = ("ingestion","briefing",
// "distill")) can emit engine events for all three kinds over /api/events. The
// SPA indexes ENGINES[kind] when it renders the live badge, queue rows and log
// header — so a kind missing from ENGINES crashes the top bar the moment that
// engine runs. Regression: `distill` was omitted from EngineKind/ENGINES and any
// distill run threw. Keep ENGINES total over every kind the server can emit.
const WIRE_ENGINE_KINDS = ['ingestion', 'briefing', 'distill'] as const

describe('ENGINES covers every engine kind the server can emit', () => {
  it.each(WIRE_ENGINE_KINDS)('has a renderable meta entry for %s', (kind) => {
    const meta = ENGINES[kind]
    expect(meta).toBeDefined()
    expect(meta.label).toBeTruthy()
    expect(meta.color).toBeTruthy()
    expect(meta.running).toBeTruthy()
  })
})
