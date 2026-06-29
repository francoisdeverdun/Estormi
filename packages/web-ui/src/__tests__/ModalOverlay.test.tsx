/**
 * Tests for the shared ``ModalOverlay`` scaffold.
 *
 * These lock the behaviours that the per-modal unit tests don't exercise and
 * that a browser would otherwise be needed to catch: the drag-guard scrim
 * close (a press that starts inside the dialog must NOT close on mouseup over
 * the scrim), the click-vs-drag-guard distinction, plain vs LIFO-stack Escape,
 * body-scroll locking, and portal rendering.
 */
import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { ModalOverlay } from '../components/ModalOverlay'

function scrimOf(dialog: HTMLElement): HTMLElement {
  const scrim = dialog.parentElement
  if (!scrim) throw new Error('scrim not found')
  return scrim
}

describe('<ModalOverlay> drag-guard scrim close', () => {
  it('closes when the press both starts AND ends on the scrim', () => {
    const onClose = vi.fn()
    render(
      <ModalOverlay onClose={onClose} closeOnScrim="drag-guard" scrimBackground="#000">
        <span data-testid="body">content</span>
      </ModalOverlay>,
    )
    const scrim = scrimOf(screen.getByRole('dialog'))
    fireEvent.mouseDown(scrim)
    fireEvent.mouseUp(scrim)
    expect(onClose).toHaveBeenCalledTimes(1)
  })

  it('does NOT close when the press starts inside the dialog and ends on the scrim (drag-to-select)', () => {
    const onClose = vi.fn()
    render(
      <ModalOverlay onClose={onClose} closeOnScrim="drag-guard" scrimBackground="#000">
        <span data-testid="body">content</span>
      </ModalOverlay>,
    )
    const scrim = scrimOf(screen.getByRole('dialog'))
    fireEvent.mouseDown(screen.getByTestId('body')) // press begins inside
    fireEvent.mouseUp(scrim) // …drifts onto the scrim
    expect(onClose).not.toHaveBeenCalled()
  })

  it('does NOT close when the press ends inside the dialog', () => {
    const onClose = vi.fn()
    render(
      <ModalOverlay onClose={onClose} closeOnScrim="drag-guard" scrimBackground="#000">
        <span data-testid="body">content</span>
      </ModalOverlay>,
    )
    const scrim = scrimOf(screen.getByRole('dialog'))
    fireEvent.mouseDown(scrim)
    fireEvent.mouseUp(screen.getByTestId('body'))
    expect(onClose).not.toHaveBeenCalled()
  })
})

describe('<ModalOverlay> click scrim close', () => {
  it('closes on a plain scrim click but not on a dialog-body click', () => {
    const onClose = vi.fn()
    render(
      <ModalOverlay onClose={onClose} closeOnScrim="click" scrimBackground="#000">
        <span data-testid="body">content</span>
      </ModalOverlay>,
    )
    const dialog = screen.getByRole('dialog')
    fireEvent.click(screen.getByTestId('body'))
    expect(onClose).not.toHaveBeenCalled()
    fireEvent.click(scrimOf(dialog))
    expect(onClose).toHaveBeenCalledTimes(1)
  })
})

describe('<ModalOverlay> Escape', () => {
  it('plain: Escape closes', () => {
    const onClose = vi.fn()
    render(
      <ModalOverlay onClose={onClose} escape="plain" scrimBackground="#000">
        <span>x</span>
      </ModalOverlay>,
    )
    fireEvent.keyDown(document, { key: 'Escape' })
    expect(onClose).toHaveBeenCalledTimes(1)
  })

  it('lifo-stack: Escape closes only the topmost layer', () => {
    const onCloseLower = vi.fn()
    const onCloseUpper = vi.fn()
    render(
      <>
        <ModalOverlay onClose={onCloseLower} escape="lifo-stack" scrimBackground="#000">
          <span>lower</span>
        </ModalOverlay>
        <ModalOverlay onClose={onCloseUpper} escape="lifo-stack" scrimBackground="#000">
          <span>upper</span>
        </ModalOverlay>
      </>,
    )
    fireEvent.keyDown(document, { key: 'Escape' })
    expect(onCloseUpper).toHaveBeenCalledTimes(1)
    expect(onCloseLower).not.toHaveBeenCalled()
  })
})

describe('<ModalOverlay> body-scroll lock + portal', () => {
  it('locks body scroll while open and restores it on unmount', () => {
    const onClose = vi.fn()
    const { unmount } = render(
      <ModalOverlay onClose={onClose} lockBodyScroll scrimBackground="#000">
        <span>x</span>
      </ModalOverlay>,
    )
    expect(document.body.style.overflow).toBe('hidden')
    unmount()
    expect(document.body.style.overflow).toBe('')
  })

  it('renders into a document.body portal when portal is set', () => {
    const onClose = vi.fn()
    const { container } = render(
      <ModalOverlay onClose={onClose} portal scrimBackground="#000">
        <span>x</span>
      </ModalOverlay>,
    )
    // The dialog lives on document.body, not inside the render container.
    expect(container.querySelector('[role="dialog"]')).toBeNull()
    expect(screen.getByRole('dialog')).toBeInTheDocument()
  })
})
