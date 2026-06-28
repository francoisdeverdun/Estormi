import { describe, it, expect, vi } from 'vitest'
import { render, fireEvent } from '@testing-library/react'
import { PrimaryAction, GhostAction } from '../components/buttons'

describe('PrimaryAction', () => {
  it('calls onClick when pressed', () => {
    const onClick = vi.fn()
    const { getByText } = render(<PrimaryAction label="RUN" onClick={onClick} />)
    fireEvent.click(getByText('RUN'))
    expect(onClick).toHaveBeenCalledOnce()
  })

  it('is disabled when disabled=true and does not fire onClick', () => {
    const onClick = vi.fn()
    const { getByText } = render(<PrimaryAction label="RUN" disabled onClick={onClick} />)
    const btn = getByText('RUN') as HTMLButtonElement
    expect(btn.disabled).toBe(true)
    fireEvent.click(btn)
    expect(onClick).not.toHaveBeenCalled()
  })

  it('is a filled-gold hero with dark ink — never a red fill', () => {
    const { getByText } = render(<PrimaryAction label="RUN" />)
    const btn = getByText('RUN') as HTMLButtonElement
    expect(btn.style.background).toContain('--or-ancien')
    expect(btn.style.color).toBe('var(--encre)')
  })
})

describe('GhostAction', () => {
  it('renders with active state styling when active=true', () => {
    const { getByText } = render(<GhostAction label="Filter" active />)
    const btn = getByText('Filter') as HTMLButtonElement
    expect(btn.style.background).toContain('rgba')
  })

  it('calls onClick when pressed', () => {
    const onClick = vi.fn()
    const { getByText } = render(<GhostAction label="Filter" onClick={onClick} />)
    fireEvent.click(getByText('Filter'))
    expect(onClick).toHaveBeenCalledOnce()
  })

  it('renders the destructive red colour when tone="danger"', () => {
    const { getByText } = render(<GhostAction label="Delete" tone="danger" />)
    const btn = getByText('Delete') as HTMLButtonElement
    expect(btn.style.color).toBe('var(--pourpre-clair)')
    expect(btn.style.borderColor).toBe('var(--pourpre-clair)')
  })

  it('is disabled and dims its colour when disabled=true', () => {
    const onClick = vi.fn()
    const { getByText } = render(<GhostAction label="Filter" disabled onClick={onClick} />)
    const btn = getByText('Filter') as HTMLButtonElement
    expect(btn.disabled).toBe(true)
    expect(btn.style.color).toBe('var(--ink-dimmer)')
    fireEvent.click(btn)
    expect(onClick).not.toHaveBeenCalled()
  })
})
