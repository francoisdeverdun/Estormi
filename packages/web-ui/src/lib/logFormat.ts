/**
 * logFormat — shared log-line model + parser for the formatted log panes.
 *
 * The briefing's "Atelier" introduced a readable log rendering (time · tag ·
 * message, with run-marker highlighting) that the ingestion views now reuse.
 * This module owns the pure parsing: tokenising the several log-line shapes
 * Estormi emits into a uniform {@link LogLine}, optionally scoping to the
 * latest run. The renderer ({@link LogStream}) and the briefing phase parser
 * both build on this.
 *
 * Recognised line shapes:
 *   - briefing:   `[briefing] HH:MM:SS LEVEL message`
 *   - ingestion:  `[HH:MM:SS] [tag] message`  /  `[HH:MM:SS] message`
 *   - run break:  `── run 20260603-215016 ──`  (per-source) and the DAG's
 *                 `… starting daily ingestion DAG at …`
 */

export type LogLevel = 'INFO' | 'WARN' | 'ERROR' | 'OK' | 'OTHER'

export interface LogLine {
  /** `HH:MM:SS`, or "" for a run-separator line. */
  time: string
  /** Gutter channel — a level (INFO) or a source tag (notes, dag, connectors). */
  tag: string
  /** Drives the line's colour. */
  level: LogLevel
  message: string
  /** A run-boundary line — rendered as a full-width separator. */
  isRunBreak: boolean
  /** A noteworthy line (phase/stage transition) — highlighted. */
  isMarker: boolean
}

export interface ParseOpts {
  /** When set, keep only lines from the last occurrence of this marker on. */
  runStartRe?: RegExp
  /** Lines whose message matches are flagged for highlight. */
  markerRe?: RegExp
  /** Lines matching are dropped entirely (e.g. HTTP transport noise). */
  dropRe?: RegExp
}

// Engine logs are prefixed with the engine name: "[briefing] HH:MM:SS LEVEL msg"
// and "[distill] HH:MM:SS LEVEL msg" share one shape.
const ENGINE_RE = /^\[(?:briefing|distill)\]\s+(\d{1,2}:\d{2}:\d{2})\s+([A-Z]+)\s+(.*)$/
const TS_TAG_RE = /^\[(\d{1,2}:\d{2}:\d{2})\]\s+(?:\[([^\]]+)\]\s+)?(.*)$/
const RUN_BREAK_RE = /^──\s*run\b/

function mapNamedLevel(raw: string): LogLevel {
  if (raw === 'INFO') return 'INFO'
  if (raw === 'WARNING' || raw === 'WARN') return 'WARN'
  if (raw === 'ERROR' || raw === 'CRITICAL') return 'ERROR'
  return 'OTHER'
}

/** Infer a level for tag-style ingestion lines that carry no explicit level. */
function inferLevel(message: string): LogLevel {
  const m = message.toLowerCase()
  // A genuine failure: a non-zero failed/error count, an explicit failure verb,
  // or a stack-trace marker. A "(0 failed)" / "0 chunks (0 failed)" summary is
  // success, so it must NOT trip the error colour.
  const failed = /\b[1-9]\d* (failed|errors?)\b/.test(m) || /✗|✘|traceback|exception|\berror:|\bfailed (to|—|-|:)/.test(m)
  if (failed) return 'ERROR'
  if (/\bok\b|\bdone\b|✓|complete|completed|success/.test(m)) return 'OK'
  if (/\bwarn/.test(m)) return 'WARN'
  return 'OTHER'
}

/** Tokenise one raw line into a partial LogLine, or null to drop it. */
function tokenize(text: string): Omit<LogLine, 'isMarker'> | null {
  if (RUN_BREAK_RE.test(text)) {
    return { time: '', tag: 'run', level: 'OTHER', message: text.replace(/─/g, '').trim(), isRunBreak: true }
  }
  // httpx logs every request at INFO ("… HTTP Request: POST … 200 OK"). For an
  // engine like distill that fans out many fetch_around calls during harvest,
  // this floods the log view with transport noise — drop it so the meaningful
  // phase lines (harvest, dataset, training) are readable.
  if (/HTTP Request:/.test(text)) return null
  const engine = text.match(ENGINE_RE)
  if (engine) {
    return {
      time: engine[1],
      tag: engine[2],
      level: mapNamedLevel(engine[2]),
      message: engine[3],
      isRunBreak: false,
    }
  }
  const tagged = text.match(TS_TAG_RE)
  if (tagged) {
    const message = tagged[3]
    return { time: tagged[1], tag: tagged[2] || '', level: inferLevel(message), message, isRunBreak: false }
  }
  return null
}

/** Keep only the lines from the last `runStartRe` match onward. */
export function scopeToLatestRun(rawLines: string[], runStartRe: RegExp): string[] {
  let start = -1
  rawLines.forEach((l, i) => {
    if (runStartRe.test(l)) start = i
  })
  return start >= 0 ? rawLines.slice(start) : rawLines
}

/** Parse a raw log tail into uniform, render-ready {@link LogLine}s. */
export function parseLogLines(raw: string, opts: ParseOpts = {}): LogLine[] {
  let rawLines = (raw || '').split('\n')
  if (opts.runStartRe) rawLines = scopeToLatestRun(rawLines, opts.runStartRe)

  const out: LogLine[] = []
  for (const rl of rawLines) {
    const text = rl.trimEnd()
    if (!text) continue
    if (opts.dropRe && opts.dropRe.test(text)) continue
    const tok = tokenize(text)
    if (!tok) continue
    const isMarker = tok.isRunBreak || (opts.markerRe ? opts.markerRe.test(tok.message) : false)
    out.push({ ...tok, isMarker })
  }
  return out
}

// ── Shared marker / run-start patterns for the ingestion surfaces ───────────────

/** The DAG launcher logs this once at the top of each global run. */
export const DAG_RUN_START = /starting daily ingestion DAG/
/** Per-source logs separate runs with a `── run <id> ──` line. */
export const SOURCE_RUN_START = /^──\s*run\b/
/** Highlight stage/run transitions in the DAG log. */
export const DAG_MARKER = /===|starting daily ingestion DAG/
/** Highlight stage boundaries + step headers in a per-source log. */
export const SOURCE_MARKER = /:\s*(starting|ok|fail)\b|Done\b|Step \d|===/
