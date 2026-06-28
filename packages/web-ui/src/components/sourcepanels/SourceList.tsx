/**
 * SourceList — the shared "tagged list" scaffold for the per-source Manage
 * panels (Apple Calendar, WhatsApp, …).
 *
 * Owns the common loading / error / empty / rows layout that each list-style
 * panel used to inline verbatim: an `Eyebrow` header, an optional error line, a
 * "Loading …" placeholder while `items === null`, an empty-state message, and
 * the flex-column of rows (plus an optional footer such as a pager). Each panel
 * stays a thin configuration of its own data fetch + per-row rendering —
 * mirroring how GoogleCalendarPanel delegates to useGoogleCalendar +
 * GCalCalendarList.
 */
import type { ReactNode } from 'react'
import { Eyebrow } from './shared'

const messageStyle: React.CSSProperties = {
  padding: 12,
  fontFamily: 'var(--font-ui)',
  fontSize: 14,
  color: 'var(--ink-dim)',
}

export interface SourceListProps<T> {
  /** Full eyebrow text, e.g. `Calendars (3)` — formatted by the caller so
   *  panels with bespoke counts (WhatsApp's `on / total`) stay verbatim. */
  title: string
  /** Items to render, or `null` while the first fetch is in flight. */
  items: T[] | null
  /** Load/refresh error to surface above the list, if any. */
  error?: string | null
  /** Copy shown when the fetch resolved to an empty list. */
  emptyLabel: string
  /** Placeholder shown while `items === null` (defaults to "Loading …"). */
  loadingLabel?: string
  /** Render one row. */
  renderRow: (item: T) => ReactNode
  /** Trailing content below the rows (e.g. a pager). Only shown with rows. */
  footer?: ReactNode
}

export function SourceList<T>({
  title,
  items,
  error,
  emptyLabel,
  loadingLabel = 'Loading…',
  renderRow,
  footer,
}: SourceListProps<T>) {
  return (
    <>
      <Eyebrow text={title} />
      {error && (
        <div
          style={{
            padding: 8,
            color: 'var(--rouge-clair)',
            fontFamily: 'var(--font-mono)',
            fontSize: 13,
          }}
        >
          {error}
        </div>
      )}
      {items === null ? (
        <div style={messageStyle}>{loadingLabel}</div>
      ) : items.length === 0 ? (
        <div style={messageStyle}>{emptyLabel}</div>
      ) : (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
          {items.map(renderRow)}
          {footer}
        </div>
      )}
    </>
  )
}
