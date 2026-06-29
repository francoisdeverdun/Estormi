/**
 * Shared scaffolding for the per-source "Manage" panels.
 *
 * These cross-panel UI atoms used to live inside the 2.6k-line
 * SourceManageModal.tsx; they were extracted so each source panel can live in
 * its own file under `components/sourcepanels/` and import the common pieces from
 * here (see SourceManageModal.tsx for the assembly).
 */
import { useEffect, useRef, useState } from 'react'
import { Fleuron, GhostAction, GoldToggle, Select } from '@estormi/ui-kit'

/** Cinzel small-caps section header with a gilt fleuron. */
export function Eyebrow({ text }: { text: string }) {
  return (
    <div
      style={{
        fontFamily: 'var(--font-display)',
        fontSize: 11,
        letterSpacing: '0.28em',
        color: 'var(--or-ancien)',
        textTransform: 'uppercase',
        marginBottom: 8,
        display: 'flex',
        alignItems: 'center',
        gap: 6,
      }}
    >
      <Fleuron size={6} /> {text}
    </div>
  )
}

export const KIND_OPTIONS = [
  'unknown',
  'work',
  'family',
  'couple',
  'friends',
  'organisation',
  'charity',
  'sport',
] as const

/** A togglable chat/calendar row with a life-context tag selector. */
export function ChatRow({
  name,
  kind,
  chatKind,
  onKind,
  options = KIND_OPTIONS,
}: {
  name: string
  kind: string
  chatKind?: string | null
  onKind: (k: string) => void
  options?: readonly string[]
}) {
  const [on, setOn] = useState(kind !== 'noise')
  // Remember the last real tag so toggling off→on restores it instead of
  // resetting to 'unknown' (which would silently discard the user's category).
  const lastTag = useRef(kind !== 'noise' ? kind : 'unknown')
  useEffect(() => {
    setOn(kind !== 'noise')
    if (kind !== 'noise') lastTag.current = kind
  }, [kind])
  return (
    <div
      style={{
        display: 'flex',
        alignItems: 'center',
        gap: 10,
        padding: '8px 10px',
        background: 'var(--well-deep)',
        border: '1px solid var(--gilt-line)',
        opacity: on ? 1 : 0.6,
      }}
    >
      <GoldToggle
        checked={on}
        onChange={(next) => {
          setOn(next)
          onKind(next ? lastTag.current : 'noise')
        }}
        ariaLabel={`Include ${name}`}
        size="sm"
      />
      <div style={{ flex: 1, minWidth: 0 }}>
        <div
          style={{
            fontFamily: 'var(--font-ui)',
            fontSize: 14,
            color: 'var(--parchemin)',
            fontWeight: 500,
            overflow: 'hidden',
            textOverflow: 'ellipsis',
            whiteSpace: 'nowrap',
          }}
          title={name}
        >
          {name}
        </div>
      </div>
      {chatKind && chatKind !== 'unknown' && (
        <span
          title="Chat kind (structural, derived from WhatsApp)"
          style={{
            fontFamily: 'var(--font-display)',
            fontSize: 10,
            letterSpacing: '0.16em',
            textTransform: 'uppercase',
            color: 'var(--or-ancien)',
            border: '1px solid var(--gilt-line)',
            borderRadius: 2,
            padding: '2px 6px',
            whiteSpace: 'nowrap',
          }}
        >
          {chatKind}
        </span>
      )}
      <Select
        uiSize="sm"
        value={options.includes(kind) ? kind : 'unknown'}
        onChange={(e) => onKind(e.target.value)}
        aria-label={`Tag · ${name}`}
      >
        {options.map((k) => (
          <option key={k} value={k}>
            {k === 'unknown' ? '— tag —' : k}
          </option>
        ))}
      </Select>
    </div>
  )
}

/** Outlined rouge button for destructive actions (disconnect, reset, wipe). */
export function DangerButton({
  label,
  disabled,
  onClick,
}: {
  label: string
  disabled?: boolean
  onClick: () => void
}) {
  return <GhostAction tone="danger" label={label} disabled={disabled} onClick={onClick} />
}
