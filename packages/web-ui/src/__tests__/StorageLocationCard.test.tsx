/**
 * Tests for ``StorageLocationCard`` — the single root storage-location knob.
 *
 * Controlled component: the parent (MaintenanceCard) supplies ``loc``. Covers:
 * the selector shows the current path and Move is disabled until a different
 * folder is chosen; choosing a folder (native picker) then "Move library" calls
 * ``relocateStorage`` and shows the "reopen to finish" notice; error path.
 */
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { StorageLocationCard } from '../components/StorageLocationCard'
import { relocateStorage } from '../api/storage'
import { pickFolder } from '../api/sources_ext'

vi.mock('../api/storage', () => ({ relocateStorage: vi.fn() }))
vi.mock('../api/sources_ext', () => ({ pickFolder: vi.fn() }))

const LOCATION = {
  dir: '/Users/me/Library/Application Support/Estormi',
  default: '/Users/me/Library/Application Support/Estormi',
  freeGb: 207,
  libraryBytes: 5 * 1024 ** 3,
  pending: null as string | null,
}

beforeEach(() => {
  vi.mocked(relocateStorage).mockReset()
  vi.mocked(pickFolder).mockReset()
})

describe('<StorageLocationCard>', () => {
  it('shows the current location and disables Move until a folder is chosen', () => {
    render(<StorageLocationCard loc={LOCATION} onRelocated={vi.fn()} />)
    expect(screen.getByLabelText('Storage location')).toHaveValue(LOCATION.dir)
    expect(screen.getByRole('button', { name: 'Move library' })).toBeDisabled()
  })

  it('picks a folder then queues a move and shows the reopen notice', async () => {
    vi.mocked(pickFolder).mockResolvedValue({ path: '/Volumes/External/Estormi' })
    vi.mocked(relocateStorage).mockResolvedValue({
      ok: true,
      willMoveOnRestart: true,
      from: LOCATION.dir,
      to: '/Volumes/External/Estormi',
      bytes: LOCATION.libraryBytes,
    })
    const onRelocated = vi.fn()
    render(<StorageLocationCard loc={LOCATION} onRelocated={onRelocated} />)

    fireEvent.click(screen.getByRole('button', { name: /choose folder/i }))
    await waitFor(() =>
      expect(screen.getByLabelText('Storage location')).toHaveValue('/Volumes/External/Estormi'),
    )
    // Move library opens a warning dialog (restart required) before queuing.
    fireEvent.click(screen.getByRole('button', { name: 'Move library' }))
    expect(screen.getByText(/quit and reopen the app/i)).toBeInTheDocument()
    expect(relocateStorage).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole('button', { name: 'Queue move' }))

    await waitFor(() => expect(relocateStorage).toHaveBeenCalledWith('/Volumes/External/Estormi'))
    await waitFor(() =>
      expect(screen.getByText(/reopen Estormi to move your library/i)).toBeInTheDocument(),
    )
    expect(onRelocated).toHaveBeenCalled()
  })

  it('surfaces the server error when the relocate is rejected', async () => {
    vi.mocked(pickFolder).mockResolvedValue({ path: '/Volumes/Tiny/x' })
    vi.mocked(relocateStorage).mockRejectedValue(
      new Error('not enough free space (2.0 GB free, need ≥5.5 GB)'),
    )
    render(<StorageLocationCard loc={LOCATION} onRelocated={vi.fn()} />)

    fireEvent.click(screen.getByRole('button', { name: /choose folder/i }))
    await waitFor(() =>
      expect(screen.getByLabelText('Storage location')).toHaveValue('/Volumes/Tiny/x'),
    )
    fireEvent.click(screen.getByRole('button', { name: 'Move library' }))
    fireEvent.click(screen.getByRole('button', { name: 'Queue move' }))

    await waitFor(() => expect(screen.getByText(/not enough free space/i)).toBeInTheDocument())
  })
})
