/**
 * SplashScreen — initial-load cover for the Mac one-pager.
 *
 * Displayed from the moment the window paints until the snapshot cache has
 * been warmed by ``prefetchAll()`` AND the sidecar's ``/health`` answers ok.
 * The previous boot showed a black/empty window during that gap; the user
 * wanted a richer cover so the app feels like it has woken up, not stalled.
 *
 * Composition (matches the manuscript voice of the rest of the SPA):
 *   - dark inked backdrop (same charbon used by AppFrame)
 *   - the canonical EstormiMasthead (mark + STORMI + Ars Memoriae + rule),
 *     identical to the iOS Briefings masthead
 *   - subtle gold orbit spinner under the mark
 *
 * Owns no state of its own; the parent (App) decides when to unmount it.
 */
import { EstormiMasthead } from '@estormi/ui-kit'

export function SplashScreen() {
  return (
    <div
      role="status"
      aria-label="Estormi loading"
      style={{
        position: 'fixed',
        inset: 0,
        zIndex: 1000,
        background:
          'radial-gradient(circle at 50% 40%, var(--encre-haut) 0%, var(--encre) 75%)',
        display: 'flex',
        flexDirection: 'column',
        alignItems: 'center',
        justifyContent: 'center',
        gap: 28,
        // Smooth fade once the parent flips the `visible` prop off via unmount —
        // exit animation is owned by App.tsx via a CSS transition on opacity.
        transition: 'opacity 220ms ease-out',
      }}
    >
      <div style={{ width: 340, maxWidth: '80vw' }}>
        <EstormiMasthead markSize={72} />
      </div>

      {/* Orbit spinner — a thin gold ring with one bright arc that rotates. */}
      <div
        aria-hidden="true"
        style={{
          width: 28,
          height: 28,
          border: '1.5px solid rgba(200, 169, 107, 0.18)',
          borderTopColor: 'var(--or-clair)',
          borderRadius: '50%',
          animation: 'estormi-splash-spin 1.1s linear infinite',
        }}
      />

      <style>{`
        @keyframes estormi-splash-spin {
          from { transform: rotate(0deg); }
          to { transform: rotate(360deg); }
        }
      `}</style>
    </div>
  )
}
