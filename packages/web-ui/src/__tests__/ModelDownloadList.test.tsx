/**
 * Tests for ``ModelDownloadList`` delete flow.
 *
 * Regression guard: deletion must go through the app's own confirm Modal, not
 * window.confirm (a silent no-op inside the Tauri WKWebView — the Delete button
 * appeared to "do nothing"). Covers: clicking Delete opens the dialog without
 * deleting, Cancel aborts, and confirming calls onDelete(key) then onChanged.
 */
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { ModelDownloadList, type DownloadListItem } from '../components/ModelDownloadList'

const ITEM: DownloadListItem = {
  key: 'ministral3-14b-estormi',
  label: 'Ministral 3 14B · Estormi SFT',
  subtitle: '7.7 GB · ≥ 16 GB',
  downloaded: true,
}

function setup(onDelete = vi.fn().mockResolvedValue(undefined), onChanged = vi.fn()) {
  render(
    <ModelDownloadList
      items={[ITEM]}
      downloadPath={(k) => `/dl/${k}`}
      onDelete={onDelete}
      deleteConfirm={(l) => `Delete ${l}? The GGUF file will be removed from disk.`}
      onChanged={onChanged}
    />,
  )
  return { onDelete, onChanged }
}

describe('<ModelDownloadList> delete', () => {
  it('opens the app confirm modal (not window.confirm) without deleting yet', () => {
    const { onDelete } = setup()
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Delete' }))
    const dialog = screen.getByRole('dialog')
    expect(within(dialog).getByText(/will be removed from disk/i)).toBeInTheDocument()
    expect(onDelete).not.toHaveBeenCalled()
  })

  it('Cancel closes the dialog without deleting', () => {
    const { onDelete } = setup()
    fireEvent.click(screen.getByRole('button', { name: 'Delete' }))
    fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'Cancel' }))
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    expect(onDelete).not.toHaveBeenCalled()
  })

  it('confirming calls onDelete with the key then onChanged', async () => {
    const onDelete = vi.fn().mockResolvedValue(undefined)
    const onChanged = vi.fn()
    setup(onDelete, onChanged)
    fireEvent.click(screen.getByRole('button', { name: 'Delete' }))
    fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'Delete' }))
    await waitFor(() => expect(onDelete).toHaveBeenCalledWith('ministral3-14b-estormi'))
    await waitFor(() => expect(onChanged).toHaveBeenCalledTimes(1))
  })
})
