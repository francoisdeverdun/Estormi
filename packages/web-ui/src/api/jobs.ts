/**
 * Jobs / scheduler API client.
 *
 * Backed by `estormi_server/api/jobs.py`. The engine room's UPCOMING section
 * reads the schedule snapshot (next cron fires + WHOOP wake-trigger state);
 * run-state itself streams over SSE via `SystemStatus`, so this stays a
 * one-shot fetch when the popover opens.
 */
import { apiGet } from './client'

export interface CronEntry {
  kind: 'ingestion' | 'briefing'
  /** ISO datetime of the next APScheduler fire, or null when unscheduled. */
  nextRun: string | null
}

export interface WhoopWakeState {
  enabled: boolean
  windowStartHour: number
  windowEndHour: number
  /** YYYY-MM-DD of the last morning the trigger fired ('' = never). */
  lastFiredDate: string
  /** ISO datetime of the poller's next check, or null when disabled. */
  nextCheck: string | null
}

export interface JobsSchedule {
  crons: CronEntry[]
  whoopWake: WhoopWakeState
}

/** Snapshot of upcoming automatic launches (crons + WHOOP wake trigger). */
export const getJobsSchedule = () => apiGet<JobsSchedule>('/api/jobs/schedule')
