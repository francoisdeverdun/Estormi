/**
 * Tests for ``GoldToggle`` — the gilt switch used across the source rows.
 *
 * Covers the interactive contract: it exposes ``role=switch`` with
 * ``aria-checked`` reflecting state, clicking fires ``onChange`` with the
 * negated value, the click is swallowed when disabled, and propagation is
 * stopped so a toggle inside a clickable row doesn't trigger the row.
 */
import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { GoldToggle } from '../components/GoldToggle'

describe('<GoldToggle>', () => {
  it('renders a switch reflecting the checked state', () => {
    render(<GoldToggle checked onChange={() => {}} ariaLabel="Enable mail" />)
    const sw = screen.getByRole('switch', { name: 'Enable mail' })
    expect(sw.getAttribute('aria-checked')).toBe('true')
  })

  it('reflects the off state', () => {
    render(<GoldToggle checked={false} onChange={() => {}} ariaLabel="Enable mail" />)
    expect(screen.getByRole('switch').getAttribute('aria-checked')).toBe('false')
  })

  it('fires onChange with the negated value on click', () => {
    const onChange = vi.fn()
    render(<GoldToggle checked={false} onChange={onChange} ariaLabel="t" />)
    fireEvent.click(screen.getByRole('switch'))
    expect(onChange).toHaveBeenCalledTimes(1)
    expect(onChange).toHaveBeenCalledWith(true)
  })

  it('does not fire onChange when disabled', () => {
    const onChange = vi.fn()
    render(<GoldToggle checked={false} onChange={onChange} disabled ariaLabel="t" />)
    const sw = screen.getByRole('switch') as HTMLButtonElement
    expect(sw.disabled).toBe(true)
    fireEvent.click(sw)
    expect(onChange).not.toHaveBeenCalled()
  })

  it('stops click propagation so an enclosing row is not also triggered', () => {
    const onChange = vi.fn()
    const rowClick = vi.fn()
    render(
      <div onClick={rowClick}>
        <GoldToggle checked={false} onChange={onChange} ariaLabel="t" />
      </div>,
    )
    fireEvent.click(screen.getByRole('switch'))
    expect(onChange).toHaveBeenCalledTimes(1)
    expect(rowClick).not.toHaveBeenCalled()
  })
})
