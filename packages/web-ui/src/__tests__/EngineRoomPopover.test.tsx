/**
 * Tests for EngineRoomPopover — the engine-room dropdown.
 *
 * Driven through the real SystemStatusProvider (a tiny harness primes the
 * running job / queue via the store's own start/setQueue). The schedule fetch
 * (UpcomingSection) and the log modal (EngineLogModal) are stubbed so the test
 * stays on the popover's own behaviour: the current-job row, the Stop action,
 * the queue list + Clear, opening logs, and outside-click / Escape dismissal.
 */
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { useEffect } from 'react'

const apiSend = vi.fn()
vi.mock('../api/client', () => ({ apiSend: (...a: unknown[]) => apiSend(...a) }))

// UpcomingSection fetches the cron schedule on mount — stub it out.
vi.mock('../components/engineroom/UpcomingSection', () => ({
  UpcomingSection: () => <div data-testid="upcoming" />,
}))
// EngineLogModal self-fetches the run log — render a marker instead so we can
// assert "logs opened" without a network call.
vi.mock('../components/engineroom/EngineLogModal', () => ({
  EngineLogModal: ({ kind, onClose }: { kind: string; onClose: () => void }) => (
    <div role="dialog" aria-label="Engine log">
      <span>log:{kind}</span>
      <button onClick={onClose}>close-log</button>
    </div>
  ),
}))

import { EngineRoomPopover } from '../components/EngineRoomPopover'
import {
  SystemStatusProvider,
  useSystemStatus,
  type EngineKind,
  type QueueEntry,
} from '../state/SystemStatus'

/** Primes the global store before the popover renders. */
function Primer({ job, queue }: { job?: EngineKind; queue?: QueueEntry[] }) {
  const sys = useSystemStatus()
  useEffect(() => {
    if (job) sys.start(job)
    if (queue) sys.setQueue(queue)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])
  return null
}

function renderPopover(opts: { job?: EngineKind; queue?: QueueEntry[] } = {}) {
  const onClose = vi.fn()
  render(
    <SystemStatusProvider>
      <Primer job={opts.job} queue={opts.queue} />
      <EngineRoomPopover onClose={onClose} />
    </SystemStatusProvider>,
  )
  return { onClose }
}

const q = (kind: EngineKind): QueueEntry => ({ kind, source: 'manual', enqueuedAt: 1_700_000_000 })

afterEach(() => {
  vi.clearAllMocks()
  vi.useRealTimers()
})

describe('<EngineRoomPopover>', () => {
  it('renders the engine-room shell with idle state and an empty queue', () => {
    renderPopover()
    expect(screen.getByRole('dialog', { name: 'Engine room' })).toBeInTheDocument()
    expect(screen.getByText('Idle')).toBeInTheDocument()
    expect(screen.getByText('« nothing waiting »')).toBeInTheDocument()
    // No Stop affordance while idle.
    expect(screen.queryByRole('button', { name: /^Stop/ })).not.toBeInTheDocument()
  })

  it('shows the running engine and stops it via /api/jobs/stop', async () => {
    // The running branch starts a 1s elapsed-clock interval; fake timers keep
    // it from ticking outside act() after the test resolves.
    vi.useFakeTimers({ shouldAdvanceTime: true })
    apiSend.mockResolvedValue(undefined)
    renderPopover({ job: 'ingestion' })

    // The Stop affordance only appears once a job is running ("Ingesting"
    // also labels the engine-grid tile, so target the unambiguous Stop button).
    const stop = await screen.findByRole('button', { name: 'Stop Ingesting' })
    fireEvent.click(stop)

    expect(apiSend).toHaveBeenCalledWith('/api/jobs/stop', 'POST', { kind: 'ingestion' })
    // Optimistic stop flips the store back to idle.
    await waitFor(() => expect(screen.getByText('Idle')).toBeInTheDocument())
  })

  it('lists queued engines and clears them via /api/jobs/queue/clear', async () => {
    apiSend.mockResolvedValue(undefined)
    renderPopover({ queue: [q('briefing'), q('distill')] })

    // Queue counter reflects the two entries.
    await waitFor(() => expect(screen.queryByText('« nothing waiting »')).not.toBeInTheDocument())
    const clear = screen.getByRole('button', { name: 'Clear' })
    fireEvent.click(clear)
    expect(apiSend).toHaveBeenCalledWith('/api/jobs/queue/clear', 'POST')
    // Let the optimistic "Clearing…" state settle back so the post-resolve
    // setState lands inside act().
    await waitFor(() => expect(clear).not.toBeDisabled())
  })

  it('opens the run log from the current/last job row', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    renderPopover({ job: 'briefing' })
    await waitFor(() => expect(screen.getByText('Composing')).toBeInTheDocument())
    // The job row itself is a button that opens the log modal.
    fireEvent.click(screen.getByTitle(/click to view logs/i))
    expect(await screen.findByText('log:briefing')).toBeInTheDocument()
  })

  it('dismisses on Escape', () => {
    const { onClose } = renderPopover()
    fireEvent.keyDown(document, { key: 'Escape' })
    expect(onClose).toHaveBeenCalled()
  })

  it('dismisses on an outside mousedown', () => {
    const { onClose } = renderPopover()
    fireEvent.mouseDown(document.body)
    expect(onClose).toHaveBeenCalled()
  })
})
