import { describe, it, expect, vi } from 'vitest'
import { render, fireEvent } from '@testing-library/react'
import { EmptyState, LoadingState, ErrorState } from '../components/states'

describe('states', () => {
  it('EmptyState shows the title and optional body', () => {
    const { getByText } = render(<EmptyState title="No sources" body="Add one in Settings" />)
    expect(getByText('No sources')).toBeTruthy()
    expect(getByText('Add one in Settings')).toBeTruthy()
  })

  it('LoadingState sets aria-busy', () => {
    const { container } = render(<LoadingState label="Working" />)
    const el = container.querySelector('[aria-busy="true"]')
    expect(el).toBeTruthy()
    expect(el?.textContent).toContain('Working')
  })

  it('ErrorState renders as role=alert and fires onRetry', () => {
    const onRetry = vi.fn()
    const { getByText, getByRole } = render(
      <ErrorState message="Boom" detail="500 internal" onRetry={onRetry} />,
    )
    expect(getByRole('alert')).toBeTruthy()
    expect(getByText('Boom')).toBeTruthy()
    expect(getByText('500 internal')).toBeTruthy()
    fireEvent.click(getByText('Retry'))
    expect(onRetry).toHaveBeenCalledOnce()
  })
})
