/**
 * AddSourceForm — register a knowledge source from the panel itself instead of
 * hand-editing the YAML. Pasting a URL is enough: the backend resolves the type
 * (YouTube/RSS), a label, and a best-guess kind — all editable before saving.
 * An optional custom prompt is stored as `pre_prompt`. Extracted from
 * KnowledgeSourcesPanel.tsx.
 */
import { useRef, useState } from 'react'
import {
  GhostAction,
  PrimaryAction,
  Select,
  TextInput,
  Textarea,
} from '@estormi/ui-kit'
import {
  resolveKnowledgeSource,
  type KnowledgeSource,
} from '../../../api/settings'
import {
  KbField,
  KB_KIND_OPTIONS,
  kbIsYouTube,
  kbSlugify,
  kbUniqueId,
} from './shared'

interface AddSourceFormProps {
  existingIds: string[]
  onAdd: (s: KnowledgeSource) => void
  onCancel: () => void
}

export function AddSourceForm({ existingIds, onAdd, onCancel }: AddSourceFormProps) {
  const [url, setUrl] = useState('')
  const [label, setLabel] = useState('')
  const [kind, setKind] = useState<string>('news')
  const [prompt, setPrompt] = useState('')
  const [resolvedType, setResolvedType] = useState<
    'youtube_channel' | 'rss' | null
  >(null)
  const [resolving, setResolving] = useState(false)
  const [note, setNote] = useState<string | null>(null)
  const lastResolved = useRef('')

  const trimmedUrl = url.trim()
  const effectiveType: 'youtube_channel' | 'rss' | null = trimmedUrl
    ? (resolvedType ?? (kbIsYouTube(trimmedUrl) ? 'youtube_channel' : 'rss'))
    : null

  const detect = async () => {
    const u = url.trim()
    if (!u || u === lastResolved.current) return
    lastResolved.current = u
    setResolving(true)
    setNote(null)
    try {
      const r = await resolveKnowledgeSource(u)
      if (!r) {
        setNote('Empty response from resolve endpoint — set the kind manually.')
        return
      }
      setResolvedType(r.type)
      setKind(r.axis)
      if (r.label) setLabel((cur) => cur.trim() || r.label)
    } catch {
      setResolvedType(kbIsYouTube(u) ? 'youtube_channel' : 'rss')
      setNote('Could not auto-detect — set the label and kind manually.')
    } finally {
      setResolving(false)
    }
  }

  const save = () => {
    const u = url.trim()
    if (!u) {
      setNote('Enter a channel or feed URL.')
      return
    }
    const type =
      effectiveType ?? (kbIsYouTube(u) ? 'youtube_channel' : 'rss')
    const finalLabel = label.trim() || u
    const src: KnowledgeSource = {
      id: kbUniqueId(kbSlugify(finalLabel), existingIds),
      label: finalLabel,
      type,
      axis: kind,
      mode: 'news',
      // Try both languages: English channels yield an `en` track, French ones
      // fall through to `fr`. A single language silently drops the other half.
      subtitle_langs: ['en', 'fr'],
    }
    if (type === 'youtube_channel') src.url = u
    else src.urls = [u]
    const p = prompt.trim()
    if (p) src.pre_prompt = p
    onAdd(src)
  }

  return (
    <div
      style={{
        display: 'flex',
        flexDirection: 'column',
        gap: 12,
        padding: 16,
        margin: '4px 0 12px',
        background: 'var(--well-mid)',
        border: '1px solid var(--gilt-line)',
      }}
    >
      <KbField label="Channel or feed URL">
        <TextInput
          value={url}
          placeholder="https://www.youtube.com/@channel — or — https://site.com/rss.xml"
          onChange={(e) => setUrl(e.target.value)}
          onBlur={() => void detect()}
          // The add-source form mounts only on an explicit user click ("Add
          // source"), and the URL field is its sole entry point — focusing it
          // is the expected next action, not a surprise focus steal.
          // eslint-disable-next-line jsx-a11y/no-autofocus
          autoFocus
        />
      </KbField>

      <div
        style={{
          display: 'flex',
          alignItems: 'center',
          gap: 10,
          fontFamily: 'var(--font-mono)',
          fontSize: 12,
          color: 'var(--ink-dim)',
          minHeight: 20,
        }}
      >
        {resolving ? (
          <span>Detecting source…</span>
        ) : effectiveType ? (
          <>
            <span style={{ color: 'var(--ink-dimmer)' }}>Detected type</span>
            <span
              style={{
                padding: '2px 8px',
                color: 'var(--or-ancien)',
                border: '1px solid var(--gilt-line)',
                textTransform: 'uppercase',
                letterSpacing: '0.12em',
              }}
            >
              {effectiveType === 'youtube_channel' ? 'YouTube' : 'RSS'}
            </span>
          </>
        ) : (
          <span style={{ color: 'var(--ink-dimmer)' }}>
            Paste a URL — type and kind are detected automatically.
          </span>
        )}
      </div>

      <KbField label="Label">
        <TextInput
          value={label}
          placeholder="Auto-filled from the source — editable"
          onChange={(e) => setLabel(e.target.value)}
        />
      </KbField>

      <KbField label="Kind">
        <Select
          style={{ textTransform: 'uppercase' }}
          value={kind}
          onChange={(e) => setKind(e.target.value)}
        >
          {KB_KIND_OPTIONS.map((k) => (
            <option key={k} value={k}>
              {k}
            </option>
          ))}
        </Select>
      </KbField>

      <KbField label="Custom prompt — optional">
        <Textarea
          style={{ minHeight: 64 }}
          value={prompt}
          placeholder="Extra guidance on how this source should be read…"
          onChange={(e) => setPrompt(e.target.value)}
        />
      </KbField>

      {note && (
        <div
          style={{
            fontFamily: 'var(--font-mono)',
            fontSize: 12,
            color: 'var(--pourpre-clair)',
          }}
        >
          {note}
        </div>
      )}

      <div style={{ display: 'flex', gap: 8, justifyContent: 'flex-end' }}>
        <GhostAction label="Cancel" size="sm" onClick={onCancel} />
        <PrimaryAction label="Add source" size="sm" onClick={save} />
      </div>
    </div>
  )
}
