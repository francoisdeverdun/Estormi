/**
 * BrandIcon — resolves a source key/name to its brand artwork.
 *
 * Backend serves brand PNGs at ``/source-icons/<slug>.png`` via the
 * FastAPI static mount (see estormi_server/server/static.py). When an icon
 * is missing on disk the <img> will 404; we swap to a Fleuron fallback
 * inside the same slot so the row still aligns.
 *
 * Naming convention follows the files shipped in ``assets/source-icons/``:
 *
 *   notes         -> apple-notes.png
 *   mail          -> apple-mail.png
 *   gcal          -> google-calendar.png  (Google Calendar)
 *   reminders     -> reminders.png
 *   whoop         -> whoop.png            (WHOOP band)
 *   imessage      -> imessage.png
 *   whatsapp      -> whatsapp.png
 *   documents     -> documents.png
 *
 * Anything else (briefings, future sources) gets a Fleuron.
 */
import { useState } from 'react'
import { Fleuron } from '@estormi/ui-kit'

export interface BrandIconProps {
  /** Canonical source key (e.g. `notes`, `gcal`). */
  source: string
  size?: number
}

const SOURCE_TO_FILE: Record<string, string> = {
  notes: 'apple-notes.png',
  mail: 'apple-mail.png',
  gcal: 'google-calendar.png',
  reminders: 'reminders.png',
  whoop: 'whoop.png',
  imessage: 'imessage.png',
  whatsapp: 'whatsapp.png',
  documents: 'documents.png',
  // `knowledge` (External knowledge / briefing feeds) intentionally has no
  // brand artwork — it isn't a third-party app — and falls back to the Fleuron.
}

function iconFileFor(source: string): string | null {
  return SOURCE_TO_FILE[source.toLowerCase()] ?? null
}

export function BrandIcon({ source, size = 28 }: BrandIconProps) {
  const file = iconFileFor(source)
  const [broken, setBroken] = useState(false)
  if (!file || broken) {
    return (
      <span
        aria-hidden
        style={{
          width: size,
          height: size,
          display: 'inline-flex',
          alignItems: 'center',
          justifyContent: 'center',
          flexShrink: 0,
        }}
      >
        <Fleuron size={Math.max(10, Math.floor(size * 0.5))} color="var(--or-ancien)" />
      </span>
    )
  }
  return (
    <img
      src={`/source-icons/${file}`}
      alt=""
      width={size}
      height={size}
      onError={() => setBroken(true)}
      style={{
        width: size,
        height: size,
        objectFit: 'contain',
        flexShrink: 0,
        display: 'block',
      }}
    />
  )
}
