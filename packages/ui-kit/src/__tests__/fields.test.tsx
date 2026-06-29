/**
 * Tests for the themed form fields — TextInput / Textarea / Select / Field.
 *
 * Covers the shared contract: native attributes pass through (value, onChange,
 * aria-label, placeholder), focus brightens the gilt border, `disabled` dims
 * and blocks input, the Select renders its custom chevron, and `Field` renders
 * its label (as a real <label> when `htmlFor` is given) + hint.
 */
import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { Field, Select, TextInput, Textarea } from '../components/fields'

describe('<TextInput>', () => {
  it('passes through value / onChange / aria-label', () => {
    const onChange = vi.fn()
    render(<TextInput value="abc" onChange={onChange} aria-label="Key" />)
    const el = screen.getByLabelText('Key') as HTMLInputElement
    expect(el.value).toBe('abc')
    fireEvent.change(el, { target: { value: 'abcd' } })
    expect(onChange).toHaveBeenCalledOnce()
  })

  it('brightens the border on focus and resets on blur', () => {
    render(<TextInput aria-label="Key" />)
    const el = screen.getByLabelText('Key') as HTMLInputElement
    expect(el.style.borderColor).toContain('--gilt-line')
    fireEvent.focus(el)
    expect(el.style.borderColor).toContain('--or-ancien')
    fireEvent.blur(el)
    expect(el.style.borderColor).toContain('--gilt-line')
  })

  it('dims and disables when disabled', () => {
    render(<TextInput aria-label="Key" disabled />)
    const el = screen.getByLabelText('Key') as HTMLInputElement
    expect(el.disabled).toBe(true)
    expect(el.style.opacity).toBe('0.45')
  })
})

describe('<Textarea>', () => {
  it('passes through value and is vertically resizable', () => {
    render(<Textarea value="prose" onChange={() => {}} aria-label="About" />)
    const el = screen.getByLabelText('About') as HTMLTextAreaElement
    expect(el.value).toBe('prose')
    expect(el.style.resize).toBe('vertical')
  })
})

describe('<Select>', () => {
  it('renders options and fires onChange with the chosen value', () => {
    const onChange = vi.fn()
    render(
      <Select value="en" onChange={onChange} aria-label="Language">
        <option value="en">English</option>
        <option value="fr">Français</option>
      </Select>,
    )
    const el = screen.getByLabelText('Language') as HTMLSelectElement
    expect(el.value).toBe('en')
    fireEvent.change(el, { target: { value: 'fr' } })
    expect(onChange).toHaveBeenCalledOnce()
  })

  it('resets the native arrow and renders a custom chevron', () => {
    render(
      <Select value="en" onChange={() => {}} aria-label="Language">
        <option value="en">English</option>
      </Select>,
    )
    const el = screen.getByLabelText('Language') as HTMLSelectElement
    expect(el.style.appearance).toBe('none')
    // The chevron is an aria-hidden glyph sibling, not in the a11y tree.
    expect(el.parentElement?.textContent).toContain('▼')
  })
})

describe('<Field>', () => {
  it('renders a div label by default', () => {
    render(
      <Field label="Objective">
        <TextInput aria-label="x" />
      </Field>,
    )
    expect(screen.getByText('Objective').tagName).toBe('DIV')
  })

  it('renders a real <label> bound to the control when htmlFor is set', () => {
    render(
      <Field label="Objective" hint="the through-line" htmlFor="obj">
        <TextInput id="obj" aria-label="x" />
      </Field>,
    )
    const label = screen.getByText('Objective')
    expect(label.tagName).toBe('LABEL')
    expect(label.getAttribute('for')).toBe('obj')
    expect(screen.getByText('the through-line')).toBeTruthy()
  })
})
