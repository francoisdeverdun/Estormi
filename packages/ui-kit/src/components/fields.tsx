/**
 * Form fields — the themed input vocabulary in the Ars Memoriae idiom.
 *
 * Before these, every `<select>` / `<input>` / `<textarea>` in the SPA was a
 * browser-default control or an ad-hoc inline `selectStyle`. These primitives
 * own the *chrome* so the whole app reads as one surface:
 *
 *   - ground   : recessed `--well-deeper` well
 *   - border   : `--gilt-line`, brightening to `--or-ancien` on focus with a
 *                faint gold glow (the "gilt focus ring")
 *   - radius   : `--radius-tight`
 *   - text     : `--parchemin`, monospace by default (the config idiom)
 *   - disabled : dimmed + not-allowed — this is how a control "greys out" when
 *                a Switch deactivates it (pass `disabled`, the field dims itself)
 *
 * Callers own *layout and typography*: pass `style` for flex sizing / minWidth,
 * or to override the font (e.g. EB Garamond for prose, Inter for free text).
 * The frame is fixed; the content font is yours.
 *
 * Each control manages its own focus state (inline styles can't express
 * `:focus-visible`, matching the hover pattern in buttons.tsx) and otherwise
 * spreads through every native attribute (value, onChange, placeholder,
 * maxLength, aria-label, …) so they are drop-in replacements.
 */
import React from 'react'

type UiSize = 'sm' | 'md'

const SIZES: Record<UiSize, { fontSize: number; padding: string }> = {
  sm: { fontSize: 10, padding: '3px 6px' },
  md: { fontSize: 11, padding: '5px 8px' },
}

/** Shared frame for every input-like control (text, select, textarea). */
function fieldFrame(disabled: boolean, focused: boolean, size: UiSize): React.CSSProperties {
  return {
    background: 'var(--well-deeper)',
    color: 'var(--parchemin)',
    border: `1px solid ${focused ? 'var(--or-ancien)' : 'var(--gilt-line)'}`,
    borderRadius: 'var(--radius-tight)',
    boxShadow: focused ? '0 0 0 2px var(--overlay-gilt-strong)' : 'none',
    fontFamily: 'var(--font-mono)',
    fontSize: SIZES[size].fontSize,
    padding: SIZES[size].padding,
    outline: 'none',
    opacity: disabled ? 0.45 : 1,
    cursor: disabled ? 'not-allowed' : 'auto',
    transition: 'border-color 140ms ease, box-shadow 140ms ease, opacity 140ms ease',
  }
}

/* ── TextInput ──────────────────────────────────────────────────────────── */

export interface TextInputProps
  extends Omit<React.InputHTMLAttributes<HTMLInputElement>, 'size'> {
  /** Visual density of the control. */
  uiSize?: UiSize
}

export const TextInput = React.forwardRef<HTMLInputElement, TextInputProps>(
  function TextInput({ uiSize = 'md', style, disabled = false, onFocus, onBlur, ...rest }, ref) {
    const [focused, setFocused] = React.useState(false)
    return (
      <input
        {...rest}
        ref={ref}
        disabled={disabled}
        onFocus={(e) => {
          setFocused(true)
          onFocus?.(e)
        }}
        onBlur={(e) => {
          setFocused(false)
          onBlur?.(e)
        }}
        style={{ ...fieldFrame(disabled, focused, uiSize), ...style }}
      />
    )
  },
)

/* ── Textarea ───────────────────────────────────────────────────────────── */

export interface TextareaProps extends React.TextareaHTMLAttributes<HTMLTextAreaElement> {
  uiSize?: UiSize
}

export const Textarea = React.forwardRef<HTMLTextAreaElement, TextareaProps>(
  function Textarea({ uiSize = 'md', style, disabled = false, onFocus, onBlur, ...rest }, ref) {
    const [focused, setFocused] = React.useState(false)
    return (
      <textarea
        {...rest}
        ref={ref}
        disabled={disabled}
        onFocus={(e) => {
          setFocused(true)
          onFocus?.(e)
        }}
        onBlur={(e) => {
          setFocused(false)
          onBlur?.(e)
        }}
        style={{
          ...fieldFrame(disabled, focused, uiSize),
          lineHeight: 1.45,
          resize: 'vertical',
          ...style,
        }}
      />
    )
  },
)

/* ── Select ─────────────────────────────────────────────────────────────────
 * The native arrow is reset (`appearance: none`) and replaced with a gilt
 * chevron so the closed control reads in-theme. The option popup stays native
 * (WebKit renders it dark under `color-scheme: dark`) — frugal and accessible.
 * Layout `style` (flex, minWidth) lands on the wrapper; the <select> fills it. */

export interface SelectProps extends React.SelectHTMLAttributes<HTMLSelectElement> {
  uiSize?: UiSize
}

export const Select = React.forwardRef<HTMLSelectElement, SelectProps>(
  function Select(
    { uiSize = 'md', style, disabled = false, onFocus, onBlur, children, ...rest },
    ref,
  ) {
    const [focused, setFocused] = React.useState(false)
    return (
      <span style={{ position: 'relative', display: 'inline-flex', ...style }}>
        <select
          {...rest}
          ref={ref}
          disabled={disabled}
          onFocus={(e) => {
            setFocused(true)
            onFocus?.(e)
          }}
          onBlur={(e) => {
            setFocused(false)
            onBlur?.(e)
          }}
          style={{
            ...fieldFrame(disabled, focused, uiSize),
            width: '100%',
            paddingRight: 22,
            appearance: 'none',
            WebkitAppearance: 'none',
            MozAppearance: 'none',
            cursor: disabled ? 'not-allowed' : 'pointer',
          }}
        >
          {children}
        </select>
        <span
          aria-hidden="true"
          style={{
            position: 'absolute',
            right: 7,
            top: '50%',
            transform: 'translateY(-50%)',
            pointerEvents: 'none',
            fontSize: 8,
            lineHeight: 1,
            color: disabled ? 'var(--ink-dimmer)' : 'var(--or-ancien)',
            opacity: disabled ? 0.45 : 1,
          }}
        >
          ▼
        </span>
      </span>
    )
  },
)

/* ── Field ──────────────────────────────────────────────────────────────────
 * Label + optional hint + control, stacked. The label is a Cinzel small-caps
 * eyebrow in gilt — the same idiom as the briefing-modal structured editor and
 * the section eyebrows. Use it whenever a control wants a heading above it. */

export interface FieldProps {
  label: React.ReactNode
  /** Smaller dim helper line under the label. */
  hint?: React.ReactNode
  /** `htmlFor` / wrapping behaviour: when set, the label is a real <label>. */
  htmlFor?: string
  children: React.ReactNode
  style?: React.CSSProperties
}

export function Field({ label, hint, htmlFor, children, style }: FieldProps) {
  const Tag = htmlFor ? 'label' : 'div'
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 3, ...style }}>
      <Tag
        {...(htmlFor ? { htmlFor } : {})}
        style={{
          fontFamily: 'var(--font-display)',
          fontSize: 10,
          letterSpacing: '0.18em',
          textTransform: 'uppercase',
          color: 'var(--or-ancien)',
        }}
      >
        {label}
      </Tag>
      {hint && <span style={{ fontSize: 11, color: 'var(--ink-dim)' }}>{hint}</span>}
      {children}
    </div>
  )
}
