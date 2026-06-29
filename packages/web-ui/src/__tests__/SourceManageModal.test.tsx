/**
 * Tests for SourceManageModal — the per-source "Manage" pane.
 *
 * Covers the modal's own behaviour (the deep source-specific LeftPanel is
 * stubbed so these stay focused): open/close gating, the historic-depth picker
 * (radiogroup → updateSettings), the danger zone two-step confirm → reset /
 * disconnect, the disconnect-availability gate, and the error surface when a
 * backend write rejects.
 */
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

const updateSettings = vi.fn()
const resetWatermark = vi.fn()
const resetSourceData = vi.fn()
const resetWhatsAppLog = vi.fn()
const disconnectWhoop = vi.fn()

vi.mock('../api/settings', () => ({
  updateSettings: (patch: unknown) => updateSettings(patch),
  resetWatermark: (k: string) => resetWatermark(k),
  resetSourceData: (k: string) => resetSourceData(k),
  resetWhatsAppLog: () => resetWhatsAppLog(),
}))

vi.mock('../api/sources_ext', () => ({
  disconnectCalendarOAuth: vi.fn(),
  resetGoogleCalendarSyncToken: vi.fn(),
  resetWhatsAppPairing: vi.fn(),
  disconnectWhoop: () => disconnectWhoop(),
}))

// The left column routes to a deep tree of self-fetching source panels; stub it
// so the test exercises the modal shell + danger zone in isolation.
vi.mock('../components/sourcemanage/LeftPanel', () => ({
  LeftPanel: () => <div data-testid="left-panel" />,
}))

import { SourceManageModal } from '../components/SourceManageModal'
import type { SourceRowDescriptor, SourceConnection } from '../components/SourceRow'

const NOTES: SourceRowDescriptor = { key: 'notes', label: 'Apple Notes', dbKey: 'notes' }
const WHOOP: SourceRowDescriptor = { key: 'whoop', label: 'WHOOP', dbKey: 'whoop' }
const REMINDERS: SourceRowDescriptor = { key: 'reminders', label: 'Reminders', dbKey: 'reminders' }

function renderModal(
  overrides: Partial<React.ComponentProps<typeof SourceManageModal>> = {},
) {
  const onClose = vi.fn()
  const onChanged = vi.fn()
  const props: React.ComponentProps<typeof SourceManageModal> = {
    open: true,
    desc: NOTES,
    connection: 'connected' as SourceConnection,
    chunks: 12,
    watermark: '2026-06-14T00:00:00Z',
    onClose,
    onChanged,
    ...overrides,
  }
  render(<SourceManageModal {...props} />)
  return { onClose, onChanged }
}

afterEach(() => {
  vi.clearAllMocks()
})

describe('<SourceManageModal>', () => {
  it('renders nothing when closed or without a descriptor', () => {
    const { container, rerender } = render(
      <SourceManageModal open={false} desc={NOTES} connection="connected" chunks={0} watermark="" onClose={vi.fn()} />,
    )
    expect(container).toBeEmptyDOMElement()
    rerender(
      <SourceManageModal open desc={null} connection="connected" chunks={0} watermark="" onClose={vi.fn()} />,
    )
    expect(container).toBeEmptyDOMElement()
  })

  it('shows the historic-depth and danger-zone sections for a depth source', () => {
    renderModal()
    expect(screen.getByRole('dialog')).toBeInTheDocument()
    expect(screen.getByRole('radiogroup', { name: 'Historic depth' })).toBeInTheDocument()
    expect(screen.getByText('Danger zone')).toBeInTheDocument()
  })

  it('persists a historic-depth choice through updateSettings', async () => {
    renderModal()
    const group = screen.getByRole('radiogroup', { name: 'Historic depth' })
    fireEvent.click(within(group).getByRole('radio', { name: '1Y' }))
    await waitFor(() =>
      expect(updateSettings).toHaveBeenCalledWith({ notes_historic_depth: '1y' }),
    )
  })

  it('closes on the footer Close button', () => {
    const { onClose } = renderModal()
    fireEvent.click(screen.getByRole('button', { name: 'Close' }))
    expect(onClose).toHaveBeenCalled()
  })

  it('reset data opens a confirm dialog and only writes after confirming', async () => {
    resetSourceData.mockResolvedValue(undefined)
    const { onChanged } = renderModal()

    fireEvent.click(screen.getByRole('button', { name: 'Reset data' }))
    const confirm = screen.getByRole('alertdialog', { name: 'Confirm destructive action' })
    expect(resetSourceData).not.toHaveBeenCalled()

    fireEvent.click(within(confirm).getByRole('button', { name: 'Confirm' }))
    await waitFor(() => expect(resetSourceData).toHaveBeenCalledWith('notes'))
    await waitFor(() => expect(onChanged).toHaveBeenCalled())
  })

  it('cancelling the danger confirm makes no backend call', () => {
    renderModal()
    fireEvent.click(screen.getByRole('button', { name: 'Reset data' }))
    const confirm = screen.getByRole('alertdialog')
    fireEvent.click(within(confirm).getByRole('button', { name: 'Cancel' }))
    expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument()
    expect(resetSourceData).not.toHaveBeenCalled()
  })

  it('disconnect is disabled for a non-disconnectable source', () => {
    renderModal({ desc: REMINDERS })
    expect(screen.getByRole('button', { name: 'Disconnect' })).toBeDisabled()
  })

  it('disconnects a WHOOP source through disconnectWhoop', async () => {
    disconnectWhoop.mockResolvedValue(undefined)
    renderModal({ desc: WHOOP })
    fireEvent.click(screen.getByRole('button', { name: 'Disconnect' }))
    const confirm = screen.getByRole('alertdialog')
    fireEvent.click(within(confirm).getByRole('button', { name: 'Confirm' }))
    await waitFor(() => expect(disconnectWhoop).toHaveBeenCalledTimes(1))
  })

  it('surfaces the backend error when a reset rejects', async () => {
    resetSourceData.mockRejectedValue(new Error('reset failed on the server'))
    renderModal()
    fireEvent.click(screen.getByRole('button', { name: 'Reset data' }))
    fireEvent.click(within(screen.getByRole('alertdialog')).getByRole('button', { name: 'Confirm' }))
    await waitFor(() =>
      expect(screen.getByRole('alert')).toHaveTextContent('reset failed on the server'),
    )
  })
})
