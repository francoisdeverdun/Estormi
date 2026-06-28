/**
 * Tests for ``ResetButton`` — the click → confirm → run → flash flow.
 *
 * Covers: the trigger opens a confirm Modal, Cancel aborts without running,
 * confirming invokes the injected ``onReset`` and then ``onDone``, and a
 * rejected reset surfaces the "! Failed" state instead of throwing.
 */
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { ResetButton } from '../components/ResetButton'

function setup(onReset: () => Promise<unknown>, onDone = vi.fn()) {
  render(
    <ResetButton
      label="Reset briefings"
      confirmTitle="Reset?"
      confirmBody="This wipes everything."
      onReset={onReset}
      onDone={onDone}
    />,
  )
  return { onDone }
}

describe('<ResetButton>', () => {
  it('opens a confirm dialog on the trigger click', () => {
    setup(() => Promise.resolve())
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Reset briefings' }))
    expect(screen.getByRole('dialog')).toBeInTheDocument()
    expect(screen.getByText('Reset?')).toBeInTheDocument()
  })

  it('Cancel closes the dialog without running the reset', () => {
    const onReset = vi.fn().mockResolvedValue(undefined)
    setup(onReset)
    fireEvent.click(screen.getByRole('button', { name: 'Reset briefings' }))
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    expect(onReset).not.toHaveBeenCalled()
  })

  it('confirming runs onReset then onDone and shows the success flash', async () => {
    const onReset = vi.fn().mockResolvedValue(undefined)
    const { onDone } = setup(onReset)
    fireEvent.click(screen.getByRole('button', { name: 'Reset briefings' }))
    fireEvent.click(screen.getByRole('button', { name: 'Reset' }))

    await waitFor(() => expect(onReset).toHaveBeenCalledTimes(1))
    await waitFor(() => expect(onDone).toHaveBeenCalledTimes(1))
    await waitFor(() =>
      expect(screen.getByRole('button', { name: '✓ Reset' })).toBeInTheDocument(),
    )
  })

  it('shows a failure state when the reset rejects', async () => {
    const onReset = vi.fn().mockRejectedValue(new Error('boom'))
    const { onDone } = setup(onReset)
    fireEvent.click(screen.getByRole('button', { name: 'Reset briefings' }))
    fireEvent.click(screen.getByRole('button', { name: 'Reset' }))

    await waitFor(() =>
      expect(screen.getByRole('button', { name: '! Failed' })).toBeInTheDocument(),
    )
    expect(onDone).not.toHaveBeenCalled()
  })
})
