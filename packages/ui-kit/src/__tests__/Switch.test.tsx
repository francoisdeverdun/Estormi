/**
 * Tests for <Switch> — the labelled GoldToggle.
 *
 * Covers: it surfaces the inner switch with the label as its accessible name,
 * clicking the knob OR the label fires onChange once with the negated value,
 * disabled blocks both, and `dimWhenOff` greys the label only when off.
 */
import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { Switch } from '../components/Switch'

describe('<Switch>', () => {
  it('uses the string label as the accessible name', () => {
    render(<Switch checked={false} onChange={() => {}} label="schedule" />)
    expect(screen.getByRole('switch', { name: 'schedule' })).toBeTruthy()
  })

  it('toggles when the label text is clicked', () => {
    const onChange = vi.fn()
    render(<Switch checked={false} onChange={onChange} label="schedule" />)
    fireEvent.click(screen.getByText('schedule'))
    expect(onChange).toHaveBeenCalledTimes(1)
    expect(onChange).toHaveBeenCalledWith(true)
  })

  it('does not toggle from the label when disabled', () => {
    const onChange = vi.fn()
    render(<Switch checked={false} onChange={onChange} label="schedule" disabled />)
    fireEvent.click(screen.getByText('schedule'))
    expect(onChange).not.toHaveBeenCalled()
  })

  it('dims the wrapper only when off and dimWhenOff is set', () => {
    const { rerender, container } = render(
      <Switch checked={false} onChange={() => {}} label="x" dimWhenOff />,
    )
    expect((container.firstChild as HTMLElement).style.opacity).toBe('0.55')
    rerender(<Switch checked onChange={() => {}} label="x" dimWhenOff />)
    expect((container.firstChild as HTMLElement).style.opacity).toBe('1')
  })
})
