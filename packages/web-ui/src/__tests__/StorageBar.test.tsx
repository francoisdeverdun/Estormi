/**
 * Tests for ``StorageBar`` — the proportional segmented storage bar.
 *
 * Covers the rendering contract: the headline total is the sum of segments
 * formatted in MB, every segment shows in the legend even at zero bytes,
 * the accessible label enumerates the breakdown, and an empty vault renders
 * without dividing by zero.
 */
import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { StorageBar, type StorageSegment } from '../components/StorageBar'

const MB = 1024 * 1024

const segments: StorageSegment[] = [
  { label: 'SQLite', bytes: 3 * MB, color: 'var(--a)' },
  { label: 'Qdrant', bytes: 1 * MB, color: 'var(--b)' },
  { label: 'Staging', bytes: 0, color: 'var(--c)' },
]

describe('<StorageBar>', () => {
  it('shows the summed total in MB', () => {
    render(<StorageBar segments={segments} />)
    // 3 + 1 + 0 = 4.0 MB
    expect(screen.getByText('4.0')).toBeInTheDocument()
    expect(screen.getByText('MB')).toBeInTheDocument()
  })

  it('lists every segment in the legend, including the zero-byte one', () => {
    render(<StorageBar segments={segments} />)
    expect(screen.getByText('SQLite')).toBeInTheDocument()
    expect(screen.getByText('Qdrant')).toBeInTheDocument()
    expect(screen.getByText('Staging')).toBeInTheDocument()
    // Per-segment MB values render in the legend.
    expect(screen.getByText('3.0')).toBeInTheDocument()
    expect(screen.getByText('1.0')).toBeInTheDocument()
  })

  it('exposes an accessible breakdown label on the bar', () => {
    render(<StorageBar segments={segments} />)
    const bar = screen.getByRole('img')
    expect(bar).toHaveAttribute(
      'aria-label',
      'Storage breakdown: SQLite 3.0 MB, Qdrant 1.0 MB, Staging 0.0 MB',
    )
  })

  it('renders an optional path label', () => {
    render(<StorageBar segments={segments} path="/Users/x/Library/Estormi" />)
    expect(screen.getByText('/Users/x/Library/Estormi')).toBeInTheDocument()
  })

  it('renders an optional detail line (footprint + free space)', () => {
    render(<StorageBar segments={segments} detail={<>16.2 GB on disk · 200.5 GB free</>} />)
    expect(screen.getByText(/16\.2 GB on disk/)).toBeInTheDocument()
    expect(screen.getByText(/200\.5 GB free/)).toBeInTheDocument()
  })

  it('handles an empty vault without crashing (total 0.0)', () => {
    render(
      <StorageBar
        segments={[{ label: 'SQLite', bytes: 0, color: 'var(--a)' }]}
      />,
    )
    // "0.0" appears for both the headline total and the single segment.
    expect(screen.getAllByText('0.0').length).toBeGreaterThanOrEqual(1)
  })
})
