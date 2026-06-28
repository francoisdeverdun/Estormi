/**
 * Shared scaffolding for the Diarium (knowledge-sources) forms — the labelled
 * field wrapper, the kind options, and the URL type/slug helpers. Extracted
 * from KnowledgeSourcesPanel.tsx so the add/edit forms can each live in their
 * own file. (The inputs themselves are now the themed @estormi/ui-kit
 * TextInput / Select / Textarea — no shared input style lives here.)
 */
import type { ReactNode } from 'react'

export const KB_KIND_OPTIONS = ['news', 'tech', 'politic', 'economic', 'finance'] as const

export function kbIsYouTube(url: string): boolean {
  return /youtube\.com|youtu\.be/i.test(url)
}

export function kbSlugify(text: string): string {
  const base = text
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '_')
    .replace(/^_+|_+$/g, '')
    .slice(0, 32)
  return base || 'source'
}

export function kbUniqueId(base: string, taken: string[]): string {
  if (!taken.includes(base)) return base
  let n = 2
  while (taken.includes(`${base}_${n}`)) n += 1
  return `${base}_${n}`
}

export function KbField({ label, children }: { label: string; children: ReactNode }) {
  return (
    <label style={{ display: 'block' }}>
      <div
        style={{
          fontFamily: 'var(--font-display)',
          fontSize: 11,
          letterSpacing: '0.22em',
          textTransform: 'uppercase',
          color: 'var(--or-ancien)',
          marginBottom: 5,
        }}
      >
        {label}
      </div>
      {children}
    </label>
  )
}
