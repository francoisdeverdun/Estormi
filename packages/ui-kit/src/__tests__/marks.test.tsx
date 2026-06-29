import { describe, it, expect } from 'vitest'
import { render } from '@testing-library/react'
import { Fleuron, Diamond } from '../components/marks'

describe('marks', () => {
  it('Fleuron renders an inline SVG with the requested number of petals', () => {
    const { container } = render(<Fleuron size={14} petals={6} />)
    const svg = container.querySelector('svg')
    expect(svg).toBeTruthy()
    expect(svg?.getAttribute('width')).toBe('14')
    expect(container.querySelectorAll('ellipse')).toHaveLength(6)
    // hasCenter defaults to true
    expect(container.querySelectorAll('circle')).toHaveLength(1)
  })

  it('Fleuron hides the centre dot when hasCenter is false', () => {
    const { container } = render(<Fleuron hasCenter={false} />)
    expect(container.querySelectorAll('circle')).toHaveLength(0)
  })

  it('Diamond renders a stroked path when not filled', () => {
    const { container } = render(<Diamond filled={false} color="#abc" />)
    const path = container.querySelector('path')
    expect(path?.getAttribute('fill')).toBe('none')
    expect(path?.getAttribute('stroke')).toBe('#abc')
  })
})
