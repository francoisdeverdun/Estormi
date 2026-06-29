/**
 * CharacterModal — your "character": the live-editable About-you profile.
 *
 * The profile (``briefing_user_context``) is trusted context handed to the
 * quills on every briefing run — who you are, the people in your life, your
 * work, what you care about, and what you expect from the briefing. It used to
 * be a cramped textarea buried in Officina; it now lives on the Summarium page
 * with a roomy modal for editing.
 *
 * Edits autosave (debounced + on-blur + on-close flush) straight into the
 * setting, and the next briefing reads it at run time — so a correction here is
 * reflected on the very next run with no extra step.
 */
import { useEffect, useRef, useState } from 'react'
import { SectionHeader, Textarea } from '@estormi/ui-kit'
import { ExtModalShell } from '../components/ExtModalShell'
import { useSettings } from '../hooks/useSettings'

const KEY = 'briefing_user_context'
const MAX = 4096
const PLACEHOLDER =
  'Who you are, the people in your life, your work, what you care about — and what ' +
  'you want from this briefing. e.g. “I’m Alex, a product designer at Acme. My ' +
  'partner is Sam; my sister Lea lives in Lyon. I care about AI and my startup’s ' +
  'funding round. Keep it short and lead with anything money- or deadline-related.”'

export function CharacterModal({ onClose }: { onClose: () => void }) {
  const { settings, save } = useSettings()
  const saved = (settings ?? {})[KEY] || ''
  const [draft, setDraft] = useState(saved)
  const [dirty, setDirty] = useState(false)
  const [state, setState] = useState<'idle' | 'saving' | 'saved' | 'error'>('idle')
  const timer = useRef<number | null>(null)
  const draftRef = useRef(draft)
  draftRef.current = draft

  // Hydrate from settings once they load — but never clobber an in-progress edit.
  useEffect(() => {
    if (!dirty) setDraft(saved)
  }, [saved, dirty])

  useEffect(
    () => () => {
      if (timer.current) window.clearTimeout(timer.current)
    },
    [],
  )

  const commit = async (next: string) => {
    setState('saving')
    try {
      await save({ [KEY]: next })
      setDirty(false)
      setState('saved')
    } catch {
      setState('error')
    }
  }

  const onChange = (value: string) => {
    setDraft(value)
    setDirty(true)
    setState('idle')
    if (timer.current) window.clearTimeout(timer.current)
    // Debounced autosave so the briefing always reflects the latest text without
    // a save button — typing settles, then it persists.
    timer.current = window.setTimeout(() => void commit(value.trim()), 700)
  }

  const flushNow = () => {
    if (timer.current) window.clearTimeout(timer.current)
    if (dirty) void commit(draftRef.current.trim())
  }

  const flushAndClose = () => {
    flushNow()
    onClose()
  }

  const statusLabel =
    state === 'saving'
      ? 'Saving…'
      : state === 'error'
        ? 'Could not save — retry'
        : dirty
          ? 'Autosaves as you type'
          : state === 'saved'
            ? 'Saved · the next briefing will use it'
            : `${draft.length}/${MAX}`

  return (
    <ExtModalShell
      onClose={flushAndClose}
      accent="var(--or-ancien)"
      ariaLabel="Character"
      maxWidth={620}
    >
      <div style={{ padding: '20px 20px 0' }}>
        <SectionHeader title="Character" letter="C" />
      </div>
      <div
        style={{
          padding: '6px 20px 20px',
          display: 'flex',
          flexDirection: 'column',
          gap: 10,
        }}
      >
        <p
          style={{
            fontFamily: 'var(--font-mono)',
            fontSize: 11,
            color: 'var(--ink-dim)',
            margin: 0,
            lineHeight: 1.55,
          }}
        >
          Who you are and what you expect from the briefing — given to the quills as trusted
          context on every run. Edits autosave; the next briefing picks them up.
        </p>
        <Textarea
          value={draft}
          placeholder={PLACEHOLDER}
          maxLength={MAX}
          onChange={(e) => onChange(e.target.value)}
          onBlur={flushNow}
          aria-label="About you"
          spellCheck={false}
          style={{ minHeight: '42vh', fontFamily: 'var(--font-body)', fontSize: 14 }}
        />
        <div
          style={{
            display: 'flex',
            justifyContent: 'flex-end',
            fontFamily: 'var(--font-mono)',
            fontSize: 10,
            color: state === 'error' ? 'var(--rouge-clair)' : 'var(--ink-dim)',
          }}
        >
          {statusLabel}
        </div>
      </div>
    </ExtModalShell>
  )
}
