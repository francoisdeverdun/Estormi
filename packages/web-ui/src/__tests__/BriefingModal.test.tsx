/**
 * Tests for the briefing editor's two modes.
 *
 * When the selected briefing carries structured `fields`, the Edit action opens
 * a labelled per-section form (Objective / Readiness / My day) and the Save
 * routes through `editBriefingFields` — the user never touches raw HTML. When a
 * briefing predates the field markers (no `fields`), it falls back to the
 * raw-HTML textarea + `editBriefing`.
 */
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

const listBriefings = vi.fn()
const getBriefing = vi.fn()
const editBriefing = vi.fn()
const editBriefingFields = vi.fn()

vi.mock('../api/knowledge', () => ({
  listBriefings: () => listBriefings(),
  getBriefing: (d: string) => getBriefing(d),
  editBriefing: (d: string, html: string) => editBriefing(d, html),
  editBriefingFields: (d: string, fields: unknown) => editBriefingFields(d, fields),
  deleteBriefing: vi.fn(),
  resetBriefings: vi.fn(),
}))

import { BriefingModal } from '../sections/BriefingModal'

const DATE = '2026-06-14'

afterEach(() => {
  vi.clearAllMocks()
})

describe('<BriefingModal> editor', () => {
  it('opens the structured form for a briefing with fields and saves via editBriefingFields', async () => {
    listBriefings.mockResolvedValue({ items: [{ date: DATE, title: 'Briefing' }] })
    getBriefing.mockResolvedValue({
      date: DATE,
      title: 'Briefing',
      htmlBody: '<p class="briefing-objective">First objective</p>',
      fields: {
        objective: 'First objective',
        readiness: 'Solid recovery',
        myDay: 'The narrative paragraph.',
      },
    })
    editBriefingFields.mockResolvedValue({ date: DATE, saved: true })

    render(<BriefingModal onClose={() => {}} />)
    await waitFor(() => expect(screen.getByText('Edit')).toBeInTheDocument())
    fireEvent.click(screen.getByText('Edit'))

    // The three labelled prose fields render — seeded from `fields`, not raw HTML.
    const objective = screen.getByLabelText('Objective') as HTMLTextAreaElement
    expect(objective.value).toBe('First objective')
    expect((screen.getByLabelText('Readiness') as HTMLTextAreaElement).value).toBe('Solid recovery')
    expect((screen.getByLabelText('My day') as HTMLTextAreaElement).value).toBe(
      'The narrative paragraph.',
    )
    // No raw-HTML editor in structured mode.
    expect(screen.queryByLabelText('Edit briefing')).toBeNull()

    fireEvent.change(objective, { target: { value: 'A corrected through-line' } })
    fireEvent.click(screen.getByText('Save'))

    await waitFor(() => expect(editBriefingFields).toHaveBeenCalledTimes(1))
    // Only the changed section is sent — untouched ones keep their server HTML.
    expect(editBriefingFields).toHaveBeenCalledWith(DATE, {
      objective: 'A corrected through-line',
    })
    expect(editBriefing).not.toHaveBeenCalled()
  })

  it('saving without changing a field makes no server call', async () => {
    listBriefings.mockResolvedValue({ items: [{ date: DATE, title: 'Briefing' }] })
    getBriefing.mockResolvedValue({
      date: DATE,
      title: 'Briefing',
      htmlBody: '<p>x</p>',
      fields: { objective: 'Untouched objective', myDay: 'Untouched prose.' },
    })

    render(<BriefingModal onClose={() => {}} />)
    await waitFor(() => expect(screen.getByText('Edit')).toBeInTheDocument())
    fireEvent.click(screen.getByText('Edit'))
    await waitFor(() => expect(screen.getByLabelText('Objective')).toBeInTheDocument())
    fireEvent.click(screen.getByText('Save'))

    await waitFor(() => expect(screen.queryByLabelText('Objective')).toBeNull()) // editor closed
    expect(editBriefingFields).not.toHaveBeenCalled()
  })

  it('falls back to the raw-HTML textarea when the briefing has no fields', async () => {
    listBriefings.mockResolvedValue({ items: [{ date: DATE, title: 'Briefing' }] })
    getBriefing.mockResolvedValue({
      date: DATE,
      title: 'Briefing',
      htmlBody: '<p>legacy body</p>',
    })
    editBriefing.mockResolvedValue({ date: DATE, saved: true })

    render(<BriefingModal onClose={() => {}} />)
    await waitFor(() => expect(screen.getByText('Edit')).toBeInTheDocument())
    fireEvent.click(screen.getByText('Edit'))

    const raw = screen.getByLabelText('Edit briefing') as HTMLTextAreaElement
    expect(raw.value).toBe('<p>legacy body</p>')
    expect(screen.queryByLabelText('Objective')).toBeNull()

    fireEvent.click(screen.getByText('Save'))
    await waitFor(() => expect(editBriefing).toHaveBeenCalledWith(DATE, '<p>legacy body</p>'))
    expect(editBriefingFields).not.toHaveBeenCalled()
  })
})
