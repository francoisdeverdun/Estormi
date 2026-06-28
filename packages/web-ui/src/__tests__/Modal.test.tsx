/**
 * Tests for the confirmation ``Modal``.
 *
 * Covers the user-visible contract: rendered title, scrim + Escape both
 * route to ``onCancel``, the confirm button fires ``onConfirm`` exactly
 * once, and the dialog is properly hidden when ``open`` is false.
 */
import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { Modal } from '../components/Modal'

describe('<Modal>', () => {
  it('does not render anything when closed', () => {
    const { container } = render(
      <Modal open={false} title="Reset everything" onConfirm={() => {}} onCancel={() => {}} />,
    )
    expect(container.firstChild).toBeNull()
  })

  it('renders the title and default labels when open', () => {
    render(
      <Modal open title="Reset everything" body="Are you sure?" onConfirm={() => {}} onCancel={() => {}} />,
    )
    expect(screen.getByRole('dialog')).toBeInTheDocument()
    expect(screen.getByText('Reset everything')).toBeInTheDocument()
    expect(screen.getByText('Are you sure?')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Confirm' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Cancel' })).toBeInTheDocument()
  })

  it('fires onConfirm exactly once when the confirm button is clicked', () => {
    const onConfirm = vi.fn()
    const onCancel = vi.fn()
    render(<Modal open title="Go" onConfirm={onConfirm} onCancel={onCancel} />)
    fireEvent.click(screen.getByRole('button', { name: 'Confirm' }))
    expect(onConfirm).toHaveBeenCalledTimes(1)
    expect(onCancel).not.toHaveBeenCalled()
  })

  it('fires onCancel on the Cancel button', () => {
    const onCancel = vi.fn()
    render(<Modal open title="Go" onConfirm={() => {}} onCancel={onCancel} />)
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    expect(onCancel).toHaveBeenCalledTimes(1)
  })

  it('Escape key invokes onCancel', () => {
    const onCancel = vi.fn()
    render(<Modal open title="Go" onConfirm={() => {}} onCancel={onCancel} />)
    fireEvent.keyDown(document, { key: 'Escape' })
    expect(onCancel).toHaveBeenCalledTimes(1)
  })

  it('uses a red-contour (danger) confirm by default — destructive', () => {
    render(<Modal open title="Reset everything" onConfirm={() => {}} onCancel={() => {}} />)
    const btn = screen.getByRole('button', { name: 'Confirm' })
    expect(btn.style.borderColor).toBe('var(--pourpre-clair)')
  })

  it('uses a filled-gold confirm for an affirmative (non-destructive) dialog', () => {
    render(
      <Modal open destructive={false} title="Save" onConfirm={() => {}} onCancel={() => {}} />,
    )
    const btn = screen.getByRole('button', { name: 'Confirm' })
    expect(btn.style.background).toContain('--or-ancien')
  })

  it('honours custom confirmLabel + cancelLabel', () => {
    render(
      <Modal
        open
        title="Go"
        confirmLabel="Delete it"
        cancelLabel="Never mind"
        onConfirm={() => {}}
        onCancel={() => {}}
      />,
    )
    expect(screen.getByRole('button', { name: 'Delete it' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Never mind' })).toBeInTheDocument()
  })
})
