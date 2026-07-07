/**
 * Tests for OnePagerTopBar — the one-pager masthead.
 *
 * Covers the build-version readout (the exact git tag on an official release,
 * else the short commit SHA, printed in small mono under the lockup) plus the
 * engine-pulse state machine: idle, running (label + elapsed), queued (+N
 * badge), the last-job "since" line, and opening the engine room.
 *
 * The engine store is driven through the real SystemStatusProvider via a tiny
 * Primer (same pattern as EngineRoomPopover.test). EngineRoomPopover is stubbed
 * so opening it doesn't fire the schedule/log fetches — this file stays on the
 * bar's own behaviour.
 */
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { useEffect } from 'react'

// The popover self-fetches (schedule, logs); stub it to a marker so the toggle
// is exercised without network.
vi.mock('../components/EngineRoomPopover', () => ({
  EngineRoomPopover: () => <div data-testid="engine-room" />,
}))

import { OnePagerTopBar } from '../components/OnePagerTopBar'
import {
  SystemStatusProvider,
  useSystemStatus,
  type EngineKind,
  type EngineStatus,
  type QueueEntry,
} from '../state/SystemStatus'
import { writeSnapshot } from '../state/snapshotCache'
import type { Overview } from '../api/overview'

/** Primes the engine store before the bar asserts against it. */
function Primer(props: {
  job?: EngineKind
  queue?: QueueEntry[]
  last?: { kind: EngineKind; endedAt: number; status: EngineStatus }
}) {
  const sys = useSystemStatus()
  useEffect(() => {
    if (props.job) sys.start(props.job)
    if (props.queue) sys.setQueue(props.queue)
    if (props.last) sys.setLastJob(props.last)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])
  return null
}

function renderBar(primer: React.ComponentProps<typeof Primer> = {}) {
  render(
    <SystemStatusProvider>
      <Primer {...primer} />
      <OnePagerTopBar />
    </SystemStatusProvider>,
  )
}

const queued = (kind: EngineKind): QueueEntry => ({
  kind,
  source: 'manual',
  enqueuedAt: 1_700_000_000,
})

describe('OnePagerTopBar build version', () => {
  beforeEach(() => {
    window.localStorage.clear()
    writeSnapshot('overview', null)
  })

  it('renders the build version from the overview snapshot', () => {
    writeSnapshot('overview', { version: 'v9.9.9-test' } as Overview)
    renderBar()
    expect(screen.getByText('v9.9.9-test')).toBeInTheDocument()
  })

  it('omits the version line when the snapshot carries no version', () => {
    writeSnapshot('overview', { data_dir: '/x' } as Overview)
    renderBar()
    // The lockup wordmark still renders; only the version slot is absent.
    expect(screen.getByText('ars memoriae')).toBeInTheDocument()
    expect(screen.queryByText(/^v\d/)).not.toBeInTheDocument()
  })
})

describe('OnePagerTopBar engine pulse', () => {
  beforeEach(() => {
    window.localStorage.clear()
    writeSnapshot('overview', null)
  })

  it('shows Idle with no recorded work and a clock when nothing is running', () => {
    renderBar()
    expect(screen.getByText('Idle')).toBeInTheDocument()
    expect(screen.getByText('no recorded work')).toBeInTheDocument()
    expect(screen.getByText(/^\d{2}:\d{2}$/)).toBeInTheDocument()
  })

  it('reflects a running engine with its label, sub-line and elapsed timer', async () => {
    renderBar({ job: 'ingestion' })
    expect(await screen.findByText('Ingesting')).toBeInTheDocument()
    expect(screen.getByText(/sources → vault/)).toBeInTheDocument()
    expect(screen.getByText('00:00')).toBeInTheDocument()
  })

  it('shows the Queued state with a +N badge', async () => {
    renderBar({ queue: [queued('briefing'), queued('distill')] })
    expect(await screen.findByText('Queued')).toBeInTheDocument()
    expect(screen.getByText('+2')).toBeInTheDocument()
  })

  it('shows the last completed engine on the sub-line when idle', async () => {
    renderBar({ last: { kind: 'briefing', endedAt: 1_700_000_000_000, status: 'ok' } })
    expect(await screen.findByText(/^briefing ·/)).toBeInTheDocument()
  })

  it('opens the engine room when the pulse is clicked', async () => {
    renderBar()
    fireEvent.click(screen.getByTitle('Open engine room'))
    await waitFor(() => expect(screen.getByTestId('engine-room')).toBeInTheDocument())
  })
})
