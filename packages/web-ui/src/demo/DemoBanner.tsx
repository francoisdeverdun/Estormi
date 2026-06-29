/**
 * Thin banner shown at the top of the app when demo mode is active.
 */
import { DEMO_MODE } from './demoInterceptor'

export function DemoBanner() {
  if (!DEMO_MODE) return null

  return (
    <div
      role="status"
      style={{
        padding: '5px 14px',
        background: 'var(--or-ancien, #8b6914)',
        color: 'var(--fond-principal, #1a1a1a)',
        fontFamily: 'var(--font-mono)',
        fontSize: 11,
        letterSpacing: '0.04em',
        textAlign: 'center',
        position: 'relative',
        zIndex: 1000,
      }}
    >
      Mode démo — données fictives
    </div>
  )
}
