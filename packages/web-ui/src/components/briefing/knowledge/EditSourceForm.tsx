/**
 * EditSourceForm — inline accordion editor for an existing knowledge source.
 * Mirrors the AddSourceForm fields but preserves the row's id so watermarks
 * survive. Extracted from KnowledgeSourcesPanel.tsx.
 */
import { useState } from 'react'
import {
  GhostAction,
  PrimaryAction,
  Select,
  TextInput,
  Textarea,
} from '@estormi/ui-kit'
import type { KnowledgeSource } from '../../../api/settings'
import { KbField, KB_KIND_OPTIONS } from './shared'

interface EditSourceFormProps {
  source: KnowledgeSource
  onSave: (patch: KnowledgeSource) => void
  onCancel: () => void
}

export function EditSourceForm({ source, onSave, onCancel }: EditSourceFormProps) {
  const isRss = source.type === 'rss'
  const [label, setLabel] = useState(source.label ?? '')
  const [url, setUrl] = useState(source.url ?? '')
  const [urlsText, setUrlsText] = useState((source.urls ?? []).join('\n'))
  const [kind, setKind] = useState<string>(source.axis ?? 'news')
  const [prompt, setPrompt] = useState(source.pre_prompt ?? '')
  const [note, setNote] = useState<string | null>(null)

  const save = () => {
    const next: KnowledgeSource = {
      ...source,
      label: label.trim() || source.label,
      axis: kind,
    }
    if (isRss) {
      const urls = urlsText
        .split('\n')
        .map((u) => u.trim())
        .filter((u) => u.length > 0)
      if (urls.length === 0) {
        setNote('Enter at least one feed URL.')
        return
      }
      next.urls = urls
      delete next.url
    } else {
      const u = url.trim()
      if (!u) {
        setNote('Enter a channel URL.')
        return
      }
      next.url = u
      delete next.urls
    }
    const p = prompt.trim()
    if (p) next.pre_prompt = p
    else delete next.pre_prompt
    onSave(next)
  }

  return (
    <div
      style={{
        display: 'flex',
        flexDirection: 'column',
        gap: 12,
        padding: 16,
        borderTop: '1px solid var(--gilt-line)',
        background: 'var(--well-mid)',
      }}
    >
      <KbField label="Label">
        <TextInput
          value={label}
          onChange={(e) => setLabel(e.target.value)}
        />
      </KbField>

      {isRss ? (
        <KbField label="Feed URLs — one per line">
          <Textarea
            style={{ minHeight: 72 }}
            value={urlsText}
            onChange={(e) => setUrlsText(e.target.value)}
          />
        </KbField>
      ) : (
        <KbField label="Channel URL">
          <TextInput
            value={url}
            onChange={(e) => setUrl(e.target.value)}
          />
        </KbField>
      )}

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
          {!KB_KIND_OPTIONS.includes(
            kind as (typeof KB_KIND_OPTIONS)[number],
          ) && <option value={kind}>{kind}</option>}
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
        <PrimaryAction label="Save" size="sm" onClick={save} />
      </div>
    </div>
  )
}
