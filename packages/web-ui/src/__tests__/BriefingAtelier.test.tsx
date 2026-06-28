/**
 * Tests for BriefingAtelier — the briefing engine's two-pane "workshop" view.
 *
 * The phase parser (`parseBriefingLog`) and the self-fetching log tail
 * (`useLogTail` / `LogStream`) are stubbed so the test drives the view's own
 * branching deterministically: the phase frieze (done/total + per-station
 * status), the loading state, the error state, and the formatted-log pane.
 */
import { render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { PhaseState } from '../lib/briefingPhases'

const parseBriefingLog = vi.fn()
const useLogTail = vi.fn()

vi.mock('../lib/briefingPhases', () => ({
  parseBriefingLog: (raw: string, running: boolean) => parseBriefingLog(raw, running),
}))

vi.mock('../components/log/LogStream', () => ({
  useLogTail: (url: string, pollMs: number) => useLogTail(url, pollMs),
  // Render the line count so the formatted-log branch is observable.
  LogStream: ({ lines }: { lines: unknown[] }) => (
    <div data-testid="log-stream">log-lines:{lines.length}</div>
  ),
}))

import { BriefingAtelier } from '../components/briefing/BriefingAtelier'

const phase = (over: Partial<PhaseState> & { id: string; label: string }): PhaseState => ({
  status: 'idle',
  ...over,
})

afterEach(() => {
  vi.clearAllMocks()
})

describe('<BriefingAtelier>', () => {
  it('renders the phase frieze with a done/total counter and the log pane', () => {
    useLogTail.mockReturnValue({ content: 'some log', loading: false, error: null })
    parseBriefingLog.mockReturnValue({
      phases: [
        phase({ id: 'sources', label: 'Sources', status: 'done' }),
        phase({ id: 'news', label: 'News', status: 'done' }),
        phase({ id: 'vault', label: 'Vault', status: 'idle' }),
      ],
      lines: [{ time: '00:00', message: 'a' }, { time: '00:01', message: 'b' }],
      done: false,
      failed: false,
    })

    render(<BriefingAtelier running={false} />)

    expect(screen.getByText('The Atelier')).toBeInTheDocument()
    // 2 of 3 phases done.
    expect(screen.getByText('2/3')).toBeInTheDocument()
    expect(screen.getByText('Sources')).toBeInTheDocument()
    expect(screen.getByText('Vault')).toBeInTheDocument()
    expect(screen.getByTestId('log-stream')).toHaveTextContent('log-lines:2')
  })

  it('polls while running and shows the loading state before any content', () => {
    useLogTail.mockReturnValue({ content: '', loading: true, error: null })
    parseBriefingLog.mockReturnValue({ phases: [], lines: [], done: false, failed: false })

    render(<BriefingAtelier running />)

    // Running passes a non-zero poll interval to the tail.
    expect(useLogTail).toHaveBeenCalledWith('/api/knowledge/log?lines=600', 2500)
    expect(screen.getByText('Loading logs…')).toBeInTheDocument()
    expect(screen.queryByTestId('log-stream')).not.toBeInTheDocument()
  })

  it('surfaces the tail error when there is no content', () => {
    useLogTail.mockReturnValue({ content: '', loading: false, error: 'log fetch failed' })
    parseBriefingLog.mockReturnValue({ phases: [], lines: [], done: false, failed: false })

    render(<BriefingAtelier running={false} />)

    expect(screen.getByText('log fetch failed')).toBeInTheDocument()
    expect(screen.queryByTestId('log-stream')).not.toBeInTheDocument()
  })

  it('passes a zero poll interval when idle (no polling)', () => {
    useLogTail.mockReturnValue({ content: 'x', loading: false, error: null })
    parseBriefingLog.mockReturnValue({ phases: [], lines: [], done: true, failed: false })

    render(<BriefingAtelier running={false} />)

    expect(useLogTail).toHaveBeenCalledWith('/api/knowledge/log?lines=600', 0)
  })
})
