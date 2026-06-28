/**
 * briefingPhases — derive the briefing engine's "agent flow" from its log.
 *
 * The briefing runs as one backend process writing a single `knowledge.log`,
 * and the SSE stream only reports start/stop — no per-phase events. But the log
 * is highly regular: each agent of the pipeline (world summarisers, news
 * synthesis, the structured extractor, enrichments, the correlation-graph
 * spine, the vision compose, the critic, the vault write) emits a stable marker
 * line. This pure module turns that raw tail into:
 *   - an ordered list of phases with a live status (idle/active/done/failed),
 *   - the parsed, de-noised log lines for a readable bottom pane.
 *
 * It is the single source of truth for the BriefingAtelier view and is unit
 * tested against real marker lines — no backend change required.
 */

import { parseLogLines, type LogLine } from './logFormat'

type PhaseStatus = 'idle' | 'active' | 'done' | 'failed'

export interface PhaseState {
  id: string
  label: string
  status: PhaseStatus
  /** A short live count pulled from the phase's marker (e.g. "4 threads"). */
  badge?: string
  /** Extra detail for a tooltip (e.g. the dominant correlation anchor). */
  detail?: string
}

/** A briefing log line is a shared {@link LogLine} plus its phase attribution. */
type BriefingLine = LogLine & { phaseId?: string }

interface BriefingTrace {
  phases: PhaseState[]
  lines: BriefingLine[]
  done: boolean
  failed: boolean
}

interface PhaseDef {
  id: string
  label: string
  /** Any line matching this marks the phase as reached. */
  marker: RegExp
  /** Optional badge/detail extractor run over the phase's matched lines. */
  badge?: (lines: string[]) => { badge?: string; detail?: string }
}

const first = (lines: string[], re: RegExp): RegExpMatchArray | null => {
  for (const l of lines) {
    const m = l.match(re)
    if (m) return m
  }
  return null
}

// The pipeline, in display order. Each agent's marker is a substring the
// orchestrator already logs (see estormi_briefing/run_briefing.py). The
// day-vision sub-agents (extractor, enrichments, correlation) run concurrently
// and their lines interleave, but their relative order is stable enough to read
// as a flow.
const PHASES: PhaseDef[] = [
  {
    id: 'sources',
    label: 'World sources',
    marker: /world corpus:|bullets parsed|items split:/,
    badge: (ls) => {
      const m = first(ls, /world corpus:.*across (\d+) source/)
      return m ? { badge: `${m[1]} sources` } : {}
    },
  },
  {
    id: 'news',
    label: 'Press synthesis',
    marker: /news_synthesis:/,
  },
  {
    id: 'extract',
    label: 'Extractor',
    marker: /extractor facts/,
    badge: (ls) => {
      const m = first(ls, /extractor facts — (\d+) physical, (\d+) partner, (\d+) open loops/)
      return m ? { badge: `${m[3]} loops`, detail: `${m[1]} activity · ${m[2]} partner · ${m[3]} loops` } : {}
    },
  },
  {
    id: 'enrich',
    label: 'Enrichments',
    marker: /enrichments:/,
    badge: (ls) => {
      const m = first(ls, /weather='([^']*)'/)
      return m && m[1] ? { detail: m[1] } : {}
    },
  },
  {
    id: 'correlate',
    label: 'Correlation',
    marker: /event correlations:|correlation graph:/,
    badge: (ls) => {
      const g = first(ls, /correlation graph: (\d+) cross-source thread/)
      const dom = first(ls, /dominant anchor='([^']*)'/)
      if (!g) return {}
      return { badge: `${g[1]} threads`, detail: dom ? `dominant: ${dom[1]}` : undefined }
    },
  },
  {
    id: 'vision',
    label: 'Vision',
    marker: /day_vision: calling LLM|day_vision: LLM returned|vision_html:/,
    badge: (ls) => {
      const m = first(ls, /(?:LLM returned|vision_html:) (\d+) chars/)
      return m ? { badge: `${m[1]} chars` } : {}
    },
  },
  {
    id: 'critic',
    label: 'Critic',
    marker: /briefing critic:/,
    badge: (ls) => {
      if (first(ls, /briefing critic: approved/)) return { badge: 'approved' }
      if (ls.some((l) => /briefing critic:/.test(l))) return { badge: 'needs review' }
      return {}
    },
  },
  {
    id: 'vault',
    label: 'Vault',
    marker: /vault write:|Done:/,
    badge: (ls) => (first(ls, /Done:/) ? { badge: 'done' } : {}),
  },
]

function phaseForMessage(message: string): string | undefined {
  for (const p of PHASES) if (p.marker.test(message)) return p.id
  return undefined
}

/**
 * The orchestrator logs this once at the very top of every run. A log tail
 * routinely spans several runs (a killed attempt, then the real one); scoping
 * to the last RUN_START keeps the parser from seeing an earlier run's terminal
 * "Done:" and painting every phase done while a fresh run is mid-flight.
 */
const RUN_START = /Starting briefing pipeline/
/** Transport noise: the per-request HTTP log lines add nothing to the story. */
const HTTP_NOISE = /HTTP Request:/

/**
 * Parse a `knowledge.log` tail into a phase flow + readable lines.
 *
 * @param raw     the log text (chronological, oldest first)
 * @param running whether the briefing engine is currently the active job —
 *                drives whether the frontier phase reads as `active` (running)
 *                or `done` (a finished run with no terminal "Done:" line).
 */
export function parseBriefingLog(raw: string, running = false): BriefingTrace {
  // Reuse the shared `[briefing] HH:MM:SS LEVEL msg` parser, scoped to the
  // latest run, then attach each line's phase attribution on top.
  const lines: BriefingLine[] = parseLogLines(raw, {
    runStartRe: RUN_START,
    dropRe: HTTP_NOISE,
  }).map((l) => {
    const phaseId = phaseForMessage(l.message)
    return { ...l, phaseId, isMarker: phaseId !== undefined }
  })
  const failed = lines.some((l) => l.level === 'ERROR')

  const done = lines.some((l) => /Done:/.test(l.message))

  // Per-phase reached + badges, computed from the messages assigned to each.
  const reached = PHASES.map((p) => lines.some((l) => p.marker.test(l.message)))
  let frontier = -1
  reached.forEach((r, i) => {
    if (r) frontier = i
  })

  const phases: PhaseState[] = PHASES.map((p, i) => {
    let status: PhaseStatus = 'idle'
    if (i < frontier) status = 'done'
    else if (i === frontier) status = done ? 'done' : running ? 'active' : 'done'
    // A failed run paints its frontier phase red rather than active.
    if (failed && i === frontier) status = 'failed'

    const matched = lines.filter((l) => p.marker.test(l.message)).map((l) => l.message)
    const extra = p.badge && matched.length ? p.badge(matched) : {}
    return { id: p.id, label: p.label, status, badge: extra.badge, detail: extra.detail }
  })

  return { phases, lines, done, failed }
}
