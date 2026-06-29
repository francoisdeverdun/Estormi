/**
 * LogStream — the shared formatted log renderer + self-fetching variant.
 *
 * Extracted from the briefing Atelier so the ingestion views render logs the
 * same way: one row per line with a time gutter, a colour-coded channel tag,
 * the message, run-separators drawn full-width, and transition markers
 * highlighted. Parsing lives in `lib/logFormat`; this file is presentation +
 * the polling fetch hook.
 */
import { useEffect, useRef, useState } from 'react'
import { apiGet } from '../../api/client'
import type { LogLevel, LogLine, ParseOpts } from '../../lib/logFormat'
import { parseLogLines } from '../../lib/logFormat'

const LEVEL_COLOR: Record<LogLevel, string> = {
  INFO: 'var(--ink-dim)',
  OK: 'var(--vert-sauge)',
  WARN: 'var(--or-clair)',
  ERROR: 'var(--rouge-clair)',
  OTHER: 'var(--ink-dim)',
}

/**
 * Self-fetching log tail with keep-last-good polling (abort + 25s deadline).
 * Returns the raw text so callers can parse it however they need.
 */
export function useLogTail(url: string, pollMs: number): { content: string; loading: boolean; error: string | null } {
  const [content, setContent] = useState('')
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let alive = true
    let firstLoad = true
    let ctl: AbortController | null = null
    let pollId: number | undefined

    const fetchOnce = () => {
      ctl = new AbortController()
      const c = ctl
      const deadline = window.setTimeout(() => c.abort(), 25000)
      apiGet<{ content?: string; error?: string }>(url, c.signal)
        .then((r) => {
          if (!alive) return
          if (r.error) setError(r.error)
          else {
            setContent(r.content ?? '')
            setError(null)
          }
        })
        .catch((e) => {
          if (!alive || !firstLoad) return
          setError(c.signal.aborted ? 'Log request timed out — backend busy.' : e instanceof Error ? e.message : String(e))
        })
        .finally(() => {
          window.clearTimeout(deadline)
          if (!alive) return
          if (firstLoad) {
            setLoading(false)
            firstLoad = false
          }
        })
    }

    fetchOnce()
    if (pollMs > 0) pollId = window.setInterval(fetchOnce, pollMs)
    return () => {
      alive = false
      ctl?.abort()
      if (pollId) window.clearInterval(pollId)
    }
  }, [url, pollMs])

  return { content, loading, error }
}

export function LogStream({ lines, autoScroll }: { lines: LogLine[]; autoScroll: boolean }) {
  const ref = useRef<HTMLDivElement>(null)
  // Newest line on top, oldest on bottom — the freshest activity is always
  // visible without scrolling. While a run is live we pin the view to the top
  // so each new line stays in sight as the tail grows above the previous ones.
  useEffect(() => {
    if (autoScroll && ref.current) ref.current.scrollTop = 0
  }, [lines, autoScroll])

  if (!lines.length) {
    return (
      <div style={{ flex: 1, padding: '12px 16px', fontSize: 12, color: 'var(--ink-dim)', fontStyle: 'italic' }}>
        No log entries yet.
      </div>
    )
  }
  const ordered = [...lines].reverse()
  return (
    <div ref={ref} style={{ flex: 1, overflow: 'auto', padding: '10px 16px' }}>
      {ordered.map((l, i) =>
        l.isRunBreak ? (
          <div
            key={i}
            style={{
              display: 'flex',
              alignItems: 'center',
              gap: 8,
              margin: '8px 0 4px',
              fontFamily: 'var(--font-display)',
              fontSize: 9,
              letterSpacing: '0.18em',
              textTransform: 'uppercase',
              color: 'var(--or-ancien)',
            }}
          >
            <span style={{ flex: 1, height: 1, background: 'var(--gilt-line)' }} />
            {l.message || 'run'}
            <span style={{ flex: 1, height: 1, background: 'var(--gilt-line)' }} />
          </div>
        ) : (
          <div
            key={i}
            style={{
              display: 'flex',
              gap: 8,
              alignItems: 'baseline',
              fontFamily: 'var(--font-mono)',
              fontSize: 11,
              lineHeight: 1.55,
              padding: l.isMarker ? '1px 6px' : '1px 0',
              marginLeft: l.isMarker ? 0 : 6,
              borderLeft: l.isMarker ? '2px solid var(--or-ancien)' : '2px solid transparent',
              background: l.isMarker ? 'rgba(201,162,77,0.06)' : undefined,
            }}
          >
            <span style={{ color: 'var(--ink-dim)', flexShrink: 0 }}>{l.time}</span>
            {l.tag && (
              <span
                style={{
                  color: LEVEL_COLOR[l.level],
                  flexShrink: 0,
                  width: 78,
                  overflow: 'hidden',
                  textOverflow: 'ellipsis',
                  whiteSpace: 'nowrap',
                }}
                title={l.tag}
              >
                {l.tag}
              </span>
            )}
            <span style={{ color: l.isMarker ? 'var(--or-clair)' : 'var(--parchemin)', wordBreak: 'break-word' }}>
              {l.message}
            </span>
          </div>
        ),
      )}
    </div>
  )
}

/**
 * FormattedLog — a self-fetching, parsed, formatted log pane. The drop-in the
 * ingestion views use in place of the old raw `<pre>` tail.
 */
export function FormattedLog({
  url,
  pollMs,
  parseOpts,
  autoScroll,
}: {
  url: string
  pollMs: number
  parseOpts?: ParseOpts
  autoScroll: boolean
}) {
  const { content, loading, error } = useLogTail(url, pollMs)
  const lines = parseLogLines(content, parseOpts)

  if (loading && !content) {
    return <div style={{ flex: 1, padding: '12px 16px', fontSize: 12, color: 'var(--ink-dim)' }}>Loading logs…</div>
  }
  if (error && !content) {
    return <div style={{ flex: 1, padding: '12px 16px', fontSize: 12, color: 'var(--rouge-clair)' }}>{error}</div>
  }
  return <LogStream lines={lines} autoScroll={autoScroll} />
}
