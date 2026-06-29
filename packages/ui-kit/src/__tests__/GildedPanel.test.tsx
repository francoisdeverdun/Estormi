import { describe, it, expect } from 'vitest'
import { render } from '@testing-library/react'
import { GildedPanel } from '../components/GildedPanel'

describe('GildedPanel', () => {
  it('renders its children', () => {
    const { getByText } = render(<GildedPanel>panel body</GildedPanel>)
    expect(getByText('panel body')).not.toBeNull()
  })

  it('uses the strong gilt frame and gold wash when gold=true', () => {
    const { container } = render(<GildedPanel gold>x</GildedPanel>)
    const panel = container.firstChild as HTMLDivElement
    // jsdom strips var() tokens from the `border` shorthand readback, but
    // keeps them on the longhand border-color — assert there.
    expect(panel.style.borderColor).toBe('var(--gilt-line-strong)')
    expect(panel.style.backgroundImage).toContain('var(--overlay-gilt)')
  })

  it('uses the gilt-line hairline when gold is omitted', () => {
    const { container } = render(<GildedPanel>x</GildedPanel>)
    const panel = container.firstChild as HTMLDivElement
    expect(panel.style.borderColor).toBe('var(--gilt-line)')
    expect(panel.style.backgroundImage).toBe('')
  })

  it('rounds the panel with the shared radius token', () => {
    const { container } = render(<GildedPanel>x</GildedPanel>)
    const panel = container.firstChild as HTMLDivElement
    expect(panel.style.borderRadius).toBe('var(--radius-panel)')
  })

  it('pads the inner content by default', () => {
    const { container } = render(<GildedPanel>x</GildedPanel>)
    const panel = container.firstChild as HTMLDivElement
    expect(panel.style.padding).toBe('22px 24px')
  })

  it('drops the padding when padded=false', () => {
    const { container } = render(<GildedPanel padded={false}>x</GildedPanel>)
    const panel = container.firstChild as HTMLDivElement
    expect(panel.style.padding).toBe('0px')
  })
})
