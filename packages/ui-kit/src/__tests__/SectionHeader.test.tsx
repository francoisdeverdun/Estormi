import { describe, it, expect } from 'vitest'
import { render } from '@testing-library/react'
import { SectionHeader } from '../components/SectionHeader'

describe('SectionHeader', () => {
  it('renders the full title in the H2 when no letter is given', () => {
    const { container } = render(<SectionHeader title="Memory" />)
    const h2 = container.querySelector('h2')!
    expect(h2.textContent).toBe('Memory')
  })

  it('drops the first letter from the H2 when the cap letter matches title[0] (integrated)', () => {
    const { container } = render(<SectionHeader title="Memory" letter="M" />)
    const h2 = container.querySelector('h2')!
    // The leading M is rendered by the IlluminatedCap, so the H2 carries the rest.
    expect(h2.textContent).toBe('emory')
  })

  it('matches the cap letter case-insensitively', () => {
    const { container } = render(<SectionHeader title="Memory" letter="m" />)
    const h2 = container.querySelector('h2')!
    expect(h2.textContent).toBe('emory')
  })

  it('keeps the whole title when the cap letter does not match title[0]', () => {
    const { container } = render(<SectionHeader title="Memory" letter="X" />)
    const h2 = container.querySelector('h2')!
    expect(h2.textContent).toBe('Memory')
  })

  it('renders the eyebrow and subtitle when provided', () => {
    const { getByText } = render(
      <SectionHeader eyebrow="ARS MEMORIAE" title="Memory" subtitle="recent activity" />,
    )
    expect(getByText('ARS MEMORIAE')).not.toBeNull()
    expect(getByText('recent activity')).not.toBeNull()
  })
})
