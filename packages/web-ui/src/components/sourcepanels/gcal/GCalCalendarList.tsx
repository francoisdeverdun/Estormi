/**
 * GCalCalendarList — the connected-state per-calendar selection + life-context
 * tagging list of GoogleCalendarPanel. Extracted from GoogleCalendarPanel.tsx.
 */
import { GoldToggle, Select } from '@estormi/ui-kit'
import { Eyebrow } from '../shared'
import {
  GCAL_GROUP_TYPES,
  type GCalCalendar,
  type GCalGroupType,
} from '../../../api/sources_ext'

export function GCalCalendarList({
  calendars,
  error,
  onToggle,
  onSetGroup,
}: {
  calendars: GCalCalendar[] | null
  error: string | null
  onToggle: (id: string, selected: boolean) => void
  onSetGroup: (id: string, group_type: GCalGroupType) => void
}) {
  return (
    <>
      <Eyebrow text={`Calendars (${calendars?.length ?? 0})`} />
      {error && (
        <div style={{ padding: 8, color: 'var(--rouge-clair)', fontSize: 13 }}>{error}</div>
      )}
      {calendars && calendars.length === 0 && (
        <div
          style={{
            padding: 12,
            fontFamily: 'var(--font-ui)',
            fontSize: 14,
            color: 'var(--ink-dim)',
          }}
        >
          Your Google account has no calendars Estormi can read.
        </div>
      )}
      {calendars && calendars.length > 0 && (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
          {calendars.map((c) => (
            <div
              key={c.id}
              style={{
                display: 'flex',
                alignItems: 'center',
                gap: 10,
                padding: '8px 10px',
                background: 'var(--encre)',
                border: '1px solid var(--gilt-line)',
              }}
            >
              <GoldToggle
                checked={c.selected}
                onChange={() => onToggle(c.id, !c.selected)}
                ariaLabel={`Toggle calendar ${c.name}`}
              />
              <span
                aria-hidden="true"
                style={{
                  width: 10,
                  height: 10,
                  borderRadius: '50%',
                  background: c.color || 'var(--or-ancien)',
                  flexShrink: 0,
                }}
              />
              <span
                style={{
                  fontFamily: 'var(--font-ui)',
                  fontSize: 16,
                  color: c.selected ? 'var(--parchemin)' : 'var(--ink-dim)',
                  flex: 1,
                  minWidth: 0,
                  overflow: 'hidden',
                  textOverflow: 'ellipsis',
                  whiteSpace: 'nowrap',
                }}
                title={c.name}
              >
                {c.name}
              </span>
              {/* Life-context tag. The themed <Select> stays accessible
                  + searchable + small. Stored server-side as the shared
                  calendar vocabulary (calendar_oauth.GCAL_GROUP_TYPES). */}
              <Select
                aria-label={`Category for ${c.name}`}
                value={c.group_type}
                onChange={(e) => onSetGroup(c.id, e.target.value as GCalGroupType)}
                style={{ flexShrink: 0 }}
              >
                {GCAL_GROUP_TYPES.map((g) => (
                  <option key={g} value={g}>
                    {g === 'unknown' ? '— tag —' : g}
                  </option>
                ))}
              </Select>
            </div>
          ))}
        </div>
      )}
    </>
  )
}
