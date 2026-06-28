/**
 * OAuthConnectionBox — the shared "Connection" block for the OAuth source
 * panels (WHOOP, Google Calendar).
 *
 * Both panels framed the same gilt-lined box around a five-state machine
 * (`probing` / `setup` / `disconnected` / `connected` / `error`) driven by
 * useOAuthConnection, differing only in service name, body copy, and the
 * source-specific setup block. This owns the box chrome and the four generic
 * states; the caller supplies the `setup` slot and the connect/disconnect/probe
 * handlers — mirroring how GoogleCalendarPanel delegates to useGoogleCalendar.
 */
import type { ReactNode } from 'react'
import { GhostAction, PrimaryAction } from '@estormi/ui-kit'
import { Eyebrow } from './shared'
import type { OAuthConnState } from './useOAuthConnection'

export interface OAuthConnectionBoxProps {
  /** Display name woven into the default copy, e.g. "WHOOP". */
  service: string
  /** Provider word for the "Connect with …" button, when it differs from
   *  `service` (Google Calendar's button reads "Connect with Google"). */
  connectWith?: string
  state: OAuthConnState
  error: string | null
  /** Re-run the status probe. */
  onProbe: () => void
  /** Open the provider consent screen in the browser. */
  onConnect: () => void
  /** Revoke the local token. */
  onDisconnect: () => void
  /** Body shown in the `disconnected` state, under the "Not connected" line. */
  disconnectedBody: ReactNode
  /** Body shown in the `connected` state, under the green confirmation line. */
  connectedBody: ReactNode
  /** Source-specific block rendered in the `setup` state. */
  setup: ReactNode
  /** Optional extra content appended inside the box (e.g. a stray error line). */
  trailing?: ReactNode
}

export function OAuthConnectionBox({
  service,
  connectWith,
  state,
  error,
  onProbe,
  onConnect,
  onDisconnect,
  disconnectedBody,
  connectedBody,
  setup,
  trailing,
}: OAuthConnectionBoxProps) {
  return (
    <>
      <Eyebrow text={'Connection'} />
      <div
        style={{
          padding: '12px 14px',
          marginBottom: 14,
          background: 'var(--encre)',
          border: '1px solid var(--gilt-line)',
          fontFamily: 'var(--font-ui)',
          fontSize: 14,
          color: 'var(--ink-dim)',
          lineHeight: 1.55,
        }}
      >
        {state === 'probing' && <>{`Checking ${service} credentials…`}</>}
        {state === 'setup' && setup}
        {state === 'disconnected' && (
          <>
            <div style={{ color: 'var(--parchemin)', fontWeight: 600, marginBottom: 6 }}>
              Not connected
            </div>
            {disconnectedBody}
            <div style={{ marginTop: 12, display: 'flex', gap: 8 }}>
              <PrimaryAction label={`Connect with ${connectWith ?? service}`} onClick={onConnect} />
              <GhostAction
                label={'I already finished — re-check'}
                size="sm"
                onClick={onProbe}
              />
            </div>
          </>
        )}
        {state === 'connected' && (
          <>
            <div style={{ color: 'var(--vert-sauge)', fontWeight: 600, marginBottom: 6 }}>
              ✓ Connected to {service}
            </div>
            {connectedBody}
            <div style={{ marginTop: 12 }}>
              <GhostAction label={'Disconnect'} size="sm" onClick={onDisconnect} />
            </div>
          </>
        )}
        {state === 'error' && (
          <>
            <div style={{ color: 'var(--rouge-clair)', fontWeight: 600, marginBottom: 6 }}>
              Error talking to {service}
            </div>
            <code style={{ fontSize: 13 }}>{error}</code>
            <div style={{ marginTop: 12 }}>
              <GhostAction label={'Retry'} size="sm" onClick={onProbe} />
            </div>
          </>
        )}
        {trailing}
      </div>
    </>
  )
}
