/**
 * OnePagerTopBar — compact narrow-column header.
 *
 * Quarter-screen layout:
 *   [E mark · ENGINE STATE · elapsed · queue+N]   [HH:MM]
 *
 * The standalone right-side LiveIndicator badge is gone — it was a
 * duplicate of the left engine pulse. Date is dropped (the system menu
 * bar already shows it); only the clock remains.
 *
 * The engine room (queue / per-engine stop / logs) opens from the left
 * engine pulse as an EngineRoomPopover rendered by this component. The
 * one-pager keeps the at-a-glance view; advanced control is one click away.
 */
import { useEffect, useState } from 'react'
import { EstormiLogoMark } from '@estormi/ui-kit'
import { EngineRoomPopover } from './EngineRoomPopover'
import { LiveDot } from './engineroom/LiveDot'
import {
  ENGINES,
  humanAgo,
  useSystemStatus,
  type EngineKind,
} from '../state/SystemStatus'

export function OnePagerTopBar() {
  const sys = useSystemStatus()
  const [now, setNow] = useState(() => new Date())
  const [elapsed, setElapsed] = useState(0)
  const [engineRoomOpen, setEngineRoomOpen] = useState(false)

  useEffect(() => {
    const id = window.setInterval(() => setNow(new Date()), 1000)
    return () => window.clearInterval(id)
  }, [])

  useEffect(() => {
    if (!sys.job || !sys.startedAt) {
      setElapsed(0)
      return
    }
    const tick = () => setElapsed(Math.floor((Date.now() - (sys.startedAt ?? 0)) / 1000))
    tick()
    const id = window.setInterval(tick, 1000)
    return () => window.clearInterval(id)
  }, [sys.job, sys.startedAt])


  const running = sys.job != null
  const meta = sys.job ? ENGINES[sys.job] : null
  const color = meta?.color ?? 'var(--vert-sauge)'
  const elapsedStr = `${String(Math.floor(elapsed / 60)).padStart(2, '0')}:${String(elapsed % 60).padStart(2, '0')}`
  const lastMeta = sys.lastJob ? ENGINES[sys.lastJob.kind] : null
  const sinceLast = sys.lastJob ? humanAgo(sys.lastJob.endedAt) : null
  const queuedN = sys.queue.length

  const stateLabel = running
    ? ENGINES[sys.job as EngineKind].running
    : queuedN > 0
      ? 'Queued'
      : 'Idle'

  const subLabel = running
    ? `${meta?.sub ?? ''} · ${elapsedStr}`
    : lastMeta && sinceLast
      ? `${lastMeta.label.toLowerCase()} · ${sinceLast}`
      : "no recorded work"

  const dateLocale = 'en-US'
  const timeStr = now.toLocaleTimeString(dateLocale, {
    hour12: false,
    hour: '2-digit',
    minute: '2-digit',
  })

  return (
    <header
      data-tauri-drag-region
      style={{
        position: 'sticky',
        top: 0,
        zIndex: 50,
        borderBottom: '1px solid var(--gilt-line-strong)',
        background:
          'linear-gradient(180deg, rgba(13,17,23,0.96) 0%, rgba(17,22,30,0.96) 100%)',
        backdropFilter: 'blur(6px)',
        WebkitBackdropFilter: 'blur(6px)',
        display: 'flex',
        alignItems: 'center',
        padding: '6px 14px',
        gap: 10,
        minHeight: 56,
      }}
    >
      {/* Brand lockup — the burgundy blocked logo mark + "STORMI" wordmark,
          sharing one identity with the iOS masthead and the app icon. */}
      <div
        style={{
          display: 'flex',
          alignItems: 'center',
          gap: 8,
          paddingRight: 10,
          borderRight: '1px solid var(--gilt-line)',
          flexShrink: 0,
        }}
      >
        <EstormiLogoMark letter="E" size={40} />
        <div style={{ display: 'flex', flexDirection: 'column', lineHeight: 1, gap: 3 }}>
          <div
            style={{
              fontFamily: 'var(--font-display)',
              fontSize: 13,
              letterSpacing: '0.24em',
              color: 'var(--or-clair)',
              textTransform: 'uppercase',
              fontWeight: 700,
            }}
          >
            stormi
          </div>
          <div
            style={{
              fontFamily: 'var(--font-display)',
              fontSize: 8,
              letterSpacing: '0.28em',
              color: 'var(--or-ancien)',
              textTransform: 'uppercase',
              fontWeight: 500,
              opacity: 0.85,
            }}
          >
            ars memoriae
          </div>
        </div>
      </div>

      {/* Engine pulse — clickable, opens the engine room popover */}
      <div style={{ position: 'relative', flex: 1, minWidth: 0 }}>
        <button
          type="button"
          onClick={() => setEngineRoomOpen((o) => !o)}
          aria-haspopup="dialog"
          aria-expanded={engineRoomOpen}
          title={
            running
              ? `${stateLabel} · click for engine room`
              : "Open engine room"
          }
          style={{
            display: 'flex',
            alignItems: 'center',
            gap: 8,
            width: '100%',
            padding: '4px 6px',
            background: engineRoomOpen ? 'rgba(255,255,255,0.04)' : 'transparent',
            border: `1px solid ${engineRoomOpen ? 'var(--gilt-line)' : 'transparent'}`,
            cursor: 'pointer',
            textAlign: 'left',
            font: 'inherit',
            color: 'inherit',
            minWidth: 0,
          }}
        >
          <LiveDot running={running} color={color} size={9} />
          <div style={{ minWidth: 0, flex: 1 }}>
            <div
              style={{
                fontFamily: 'var(--font-display)',
                fontSize: 11,
                letterSpacing: '0.18em',
                textTransform: 'uppercase',
                color: running ? color : 'var(--ink-dim)',
                fontWeight: 700,
                whiteSpace: 'nowrap',
                overflow: 'hidden',
                textOverflow: 'ellipsis',
              }}
            >
              {stateLabel}
              {running && (
                <span
                  style={{
                    marginLeft: 8,
                    fontFamily: 'var(--font-mono)',
                    fontSize: 11,
                    letterSpacing: '0.02em',
                    color: 'var(--ink-dim)',
                  }}
                >
                  {elapsedStr}
                </span>
              )}
              {queuedN > 0 && (
                <span
                  style={{
                    marginLeft: 8,
                    padding: '0 5px',
                    background: 'var(--charbon-3)',
                    border: '1px solid var(--gilt-line)',
                    color: 'var(--or-clair)',
                    fontFamily: 'var(--font-mono)',
                    fontSize: 10,
                    letterSpacing: 0,
                  }}
                >
                  +{queuedN}
                </span>
              )}
            </div>
            <div
              style={{
                fontFamily: 'var(--font-mono)',
                fontSize: 10,
                color: 'var(--ink-dim)',
                marginTop: 1,
                whiteSpace: 'nowrap',
                overflow: 'hidden',
                textOverflow: 'ellipsis',
              }}
            >
              {subLabel}
            </div>
          </div>
          <span
            aria-hidden="true"
            style={{
              fontFamily: 'var(--font-mono)',
              fontSize: 9,
              color: 'var(--ink-dim)',
              marginLeft: 4,
            }}
          >
            {engineRoomOpen ? '▴' : '▾'}
          </span>
        </button>
        {engineRoomOpen && (
          <EngineRoomPopover onClose={() => setEngineRoomOpen(false)} />
        )}
      </div>

      {/* Right cluster: the clock. */}
      <div
        style={{
          display: 'flex',
          alignItems: 'center',
          gap: 8,
          flexShrink: 0,
        }}
      >
        <div
          style={{
            fontFamily: 'var(--font-mono)',
            fontSize: 12,
            color: 'var(--parchemin)',
            letterSpacing: '0.04em',
          }}
        >
          {timeStr}
        </div>
      </div>
    </header>
  )
}
