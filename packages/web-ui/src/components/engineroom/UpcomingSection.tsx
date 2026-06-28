/**
 * UpcomingSection — the engine room's view of what will launch ITSELF next:
 * the two daily crons (ingestion, briefing) and the WHOOP wake trigger (which
 * refreshes the readiness card in ~1 minute once the night's recovery is
 * scored — or runs the full pipeline when no briefing exists yet).
 *
 * One-shot fetch when the popover opens; run-state itself streams over SSE
 * elsewhere, so there is nothing to poll here.
 */
import { useEffect, useState } from 'react'
import { Diamond } from '@estormi/ui-kit'
import { getJobsSchedule, type JobsSchedule } from '../../api/jobs'

const rowStyle: React.CSSProperties = {
  display: 'flex',
  alignItems: 'baseline',
  justifyContent: 'space-between',
  gap: 12,
  fontFamily: 'var(--font-mono)',
  fontSize: 11,
  color: 'var(--ink-dim)',
  padding: '3px 2px',
}

const whenStyle: React.CSSProperties = { color: 'var(--parchemin)' }

/** "HH:MM" today, "tomorrow HH:MM" past midnight, else "DD/MM HH:MM". */
function formatNextRun(iso: string | null, now: Date): string {
  if (!iso) return 'off'
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return 'off'
  const hm = `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`
  const sameDay = d.toDateString() === now.toDateString()
  if (sameDay) return hm
  const tomorrow = new Date(now)
  tomorrow.setDate(now.getDate() + 1)
  if (d.toDateString() === tomorrow.toDateString()) return `tomorrow ${hm}`
  return `${String(d.getDate()).padStart(2, '0')}/${String(d.getMonth() + 1).padStart(2, '0')} ${hm}`
}

function whoopLine(s: JobsSchedule['whoopWake'], now: Date): { label: string; value: string } {
  const win = `${String(s.windowStartHour).padStart(2, '0')}–${String(s.windowEndHour).padStart(2, '0')}h`
  if (!s.enabled) return { label: 'wake refresh (WHOOP)', value: 'off' }
  const today = `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, '0')}-${String(now.getDate()).padStart(2, '0')}`
  if (s.lastFiredDate === today) return { label: 'wake refresh (WHOOP)', value: '✓ fired today' }
  const inWindow = now.getHours() >= s.windowStartHour && now.getHours() < s.windowEndHour
  if (inWindow) return { label: 'wake refresh (WHOOP)', value: `armed · window ${win}` }
  return { label: 'wake refresh (WHOOP)', value: `window ${win}` }
}

export function UpcomingSection() {
  const [schedule, setSchedule] = useState<JobsSchedule | null>(null)

  useEffect(() => {
    let alive = true
    getJobsSchedule()
      .then((s) => {
        if (alive) setSchedule(s)
      })
      .catch(() => {
        /* the section simply doesn't render — never block the popover */
      })
    return () => {
      alive = false
    }
  }, [])

  if (!schedule) return null
  const now = new Date()
  const whoop = whoopLine(schedule.whoopWake, now)

  return (
    <>
      <div
        style={{
          fontFamily: 'var(--font-display)',
          fontSize: 10,
          letterSpacing: '0.24em',
          color: 'var(--or-ancien)',
          textTransform: 'uppercase',
          display: 'flex',
          alignItems: 'center',
          gap: 6,
          margin: '12px 0 4px',
        }}
      >
        <Diamond size={5} color="var(--or-ancien)" /> Upcoming
      </div>
      {schedule.crons.map((c) => (
        <div key={c.kind} style={rowStyle}>
          <span>{c.kind} cron</span>
          <span style={whenStyle}>{formatNextRun(c.nextRun, now)}</span>
        </div>
      ))}
      <div style={rowStyle}>
        <span>{whoop.label}</span>
        <span style={whenStyle}>{whoop.value}</span>
      </div>
    </>
  )
}
