/**
 * Tests for the WhatsApp panel's chat-list pagination.
 *
 * A paired account can carry hundreds of chats; the list is paged 50 at a
 * time so the modal doesn't become an endless scroll. Covers: only one page
 * renders at a time, the range indicator, and Prev/Next navigation.
 */
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

const getWhatsAppChats = vi.fn()

vi.mock('../api/sources_ext', () => ({
  getWhatsAppChats: () => getWhatsAppChats(),
  patchWhatsAppChat: vi.fn(),
  resetWhatsAppPairing: vi.fn(),
  whatsappQrUrl: () => 'about:blank',
}))

import { WhatsAppPanel } from '../components/sourcepanels/WhatsAppPanel'

function makeChats(n: number) {
  return Array.from({ length: n }, (_, i) => ({
    chat_id: `c${i}`,
    chat_name: `Chat ${i}`,
    group_type: 'unknown',
  }))
}

afterEach(() => {
  getWhatsAppChats.mockReset()
  vi.useRealTimers()
})

describe('<WhatsAppPanel> chat pagination', () => {
  it('renders only the first 50 chats with a range indicator', async () => {
    getWhatsAppChats.mockResolvedValue(makeChats(120))
    render(<WhatsAppPanel connection="connected" />)

    await waitFor(() => expect(screen.getByText('Chat 0')).toBeInTheDocument())
    expect(screen.getByText('Chat 49')).toBeInTheDocument()
    expect(screen.queryByText('Chat 50')).not.toBeInTheDocument()
    expect(screen.getByText('1–50 of 120')).toBeInTheDocument()
  })

  it('advances a page on Next and goes back on Prev', async () => {
    getWhatsAppChats.mockResolvedValue(makeChats(120))
    render(<WhatsAppPanel connection="connected" />)

    await waitFor(() => expect(screen.getByText('Chat 0')).toBeInTheDocument())

    fireEvent.click(screen.getByRole('button', { name: 'Next ›' }))
    expect(screen.getByText('51–100 of 120')).toBeInTheDocument()
    expect(screen.getByText('Chat 50')).toBeInTheDocument()
    expect(screen.queryByText('Chat 0')).not.toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: '‹ Prev' }))
    expect(screen.getByText('1–50 of 120')).toBeInTheDocument()
    expect(screen.getByText('Chat 0')).toBeInTheDocument()
  })

  it('shows no pager when the list fits on one page', async () => {
    getWhatsAppChats.mockResolvedValue(makeChats(30))
    render(<WhatsAppPanel connection="connected" />)

    await waitFor(() => expect(screen.getByText('Chat 0')).toBeInTheDocument())
    expect(screen.queryByRole('button', { name: 'Next ›' })).not.toBeInTheDocument()
    expect(screen.queryByText(/of 30/)).not.toBeInTheDocument()
  })

  it('renders the structural chat_kind as a read-only badge, but not "unknown"', async () => {
    getWhatsAppChats.mockResolvedValue([
      { chat_id: 'g', chat_name: 'Team', group_type: 'unknown', chat_kind: 'group' },
      { chat_id: 'd', chat_name: 'Alice', group_type: 'work', chat_kind: 'dm' },
      { chat_id: 'u', chat_name: 'Mystery', group_type: 'unknown', chat_kind: 'unknown' },
    ])
    render(<WhatsAppPanel connection="connected" />)

    await waitFor(() => expect(screen.getByText('Team')).toBeInTheDocument())
    // Structural kind surfaces as its own badge, independent of the semantic tag.
    expect(screen.getByText('group')).toBeInTheDocument()
    expect(screen.getByText('dm')).toBeInTheDocument()
    // 'unknown' (and absent) chat_kind shows no badge — the dropdown shows '— tag —'.
    expect(screen.queryByText('unknown')).not.toBeInTheDocument()
  })
})
