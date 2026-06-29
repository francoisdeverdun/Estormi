/**
 * App — one-pager shell.
 *
 * Layout: AppFrame backdrop + OnePagerTopBar (engine pulse) above a single
 * scrolling main column with two sections:
 *   1. CardinalSection   — metrics + the clickable counter into the briefing modal
 *   2. ParametersSection — sources + knowledge sources + maintenance card
 *
 * Boot flow: ``SplashScreen`` covers the window from the first paint until
 * BOTH the sidecar's ``/health`` answers ok AND ``prefetchAll()`` has
 * settled. That keeps the user from seeing a black redirect gap (Tauri ⇒
 * FastAPI URL) followed by empty panels — by the time the splash unmounts,
 * the snapshot cache is warm and panels render with real numbers.
 *
 * Tauri opens on the bundled dist (tauri://…) and replaces the URL with the
 * FastAPI-served one once the sidecar answers. The splash spans both
 * phases.
 */
import { useEffect, useState } from 'react'
import { SystemStatusProvider } from './state/SystemStatus'
import { EngineEventsBridge } from './state/EngineEventsBridge'
import { AppFrame } from './components/AppFrame'
import { SplashScreen } from './components/SplashScreen'
import { OnePagerTopBar } from './components/OnePagerTopBar'
import { CardinalSection } from './sections/CardinalSection'
import { ParametersSection } from './sections/ParametersSection'
import { BriefingModal } from './sections/BriefingModal'
import { CharacterModal } from './sections/CharacterModal'
import { getOverview } from './api/overview'
import { pingHealth } from './api/client'
import { writeSnapshot } from './state/snapshotCache'
import { prefetchAll } from './state/prefetch'
import { useBackendHealth } from './hooks/useBackendHealth'
import { DemoBanner } from './demo/DemoBanner'

// Minimum splash dwell so a very fast cold start doesn't flash the lockup.
// Long enough to read; short enough not to feel sluggish.
const SPLASH_MIN_MS = 600
// Hard ceiling — if the backend or prefetch hang, the splash falls away after
// this so the user always lands on the UI (the panels show their own empty/
// loading states from there).
const SPLASH_MAX_MS = 6000

// Cardinal tiles open either the briefing modal or the Character (About-you) modal.
type ModalId = 'briefing' | 'character' | null

export function App() {
  // Tauri opens on the bundled dist (tauri://…), then location.replace()s
  // to the FastAPI-served URL once the sidecar answers. The splash covers
  // both the redirect window and the cache-warm window so the user never
  // sees an empty or half-loaded panel.
  const isBundledBoot = !window.location.protocol.startsWith('http')
  const [modal, setModal] = useState<ModalId>(null)
  const [booting, setBooting] = useState(true)
  // Bumped when the Character modal closes so the Summarium tile re-reads the
  // profile and its preview stays fresh after an edit.
  const [characterRev, setCharacterRev] = useState(0)
  const closeModal = () => setModal(null)
  const closeCharacter = () => {
    setCharacterRev((v) => v + 1)
    setModal(null)
  }
  // Live sidecar reachability — only polled once we're on the http:// origin
  // (the pre-redirect tauri:// tree is torn down immediately).
  const backendReachable = useBackendHealth(!isBundledBoot)

  useEffect(() => {
    if (isBundledBoot) return
    let cancelled = false
    const startedAt = Date.now()

    // Overview snapshot — feeds the brand/footer; cheap, never blocks UI.
    getOverview()
      .then((o) => {
        if (!cancelled) writeSnapshot('overview', o)
      })
      .catch(() => {})

    // Resolve when /health answers ok — proves the sidecar is alive before
    // we hand the user a panel that tries to fetch from it.
    const healthOk = (async () => {
      const deadline = Date.now() + SPLASH_MAX_MS
      while (Date.now() < deadline) {
        if (await pingHealth()) return
        await new Promise((resolve) => setTimeout(resolve, 100))
      }
    })()

    // Drop the splash once both halves of "ready" land — health + warmed
    // cache. The min-dwell prevents a sub-100ms flash on a hot reload; the
    // max-dwell prevents a stuck backend from pinning the user on the splash.
    void Promise.all([healthOk, prefetchAll()]).finally(() => {
      const elapsed = Date.now() - startedAt
      const remaining = Math.max(0, SPLASH_MIN_MS - elapsed)
      window.setTimeout(() => {
        if (!cancelled) setBooting(false)
      }, remaining)
    })

    // Belt-and-braces fallback: never leave the user on the splash forever.
    const hardStop = window.setTimeout(() => {
      if (!cancelled) setBooting(false)
    }, SPLASH_MAX_MS + SPLASH_MIN_MS)

    return () => {
      cancelled = true
      window.clearTimeout(hardStop)
    }
  }, [isBundledBoot])

  if (isBundledBoot) {
    // Pre-redirect: just paint the splash over the backdrop. The React tree
    // gets torn down + remounted under http:// once Tauri redirects.
    return (
      <>
        <AppFrame />
        <SplashScreen />
      </>
    )
  }

  return (
    <SystemStatusProvider>
      <DemoBanner />
      <EngineEventsBridge />
      <AppFrame />
      <div
        style={{
          display: 'flex',
          flexDirection: 'column',
          minHeight: '100vh',
          position: 'relative',
          zIndex: 1,
        }}
        data-screen-label="onepager"
      >
        {!backendReachable && (
          <div
            role="alert"
            style={{
              padding: '6px 14px',
              borderBottom: '1px solid var(--rouge-clair)',
              color: 'var(--rouge-clair)',
              fontFamily: 'var(--font-mono)',
              fontSize: 11,
              letterSpacing: '0.04em',
              textAlign: 'center',
            }}
          >
            Backend not reachable — retrying…
          </div>
        )}
        <OnePagerTopBar />
        <main
          style={{
            flex: 1,
            width: '100%',
            // Quarter-screen one-pager: tight padding + small inter-section
            // gap. No max-width — the layout adapts to the narrow window.
            padding: '16px 14px 32px',
            display: 'flex',
            flexDirection: 'column',
            gap: 18,
          }}
        >
          <CardinalSection
            onOpenBriefing={() => setModal('briefing')}
            onOpenCharacter={() => setModal('character')}
            characterRev={characterRev}
          />
          <ParametersSection />
        </main>
      </div>
      {modal === 'briefing' && <BriefingModal onClose={closeModal} />}
      {modal === 'character' && <CharacterModal onClose={closeCharacter} />}
      {booting && <SplashScreen />}
    </SystemStatusProvider>
  )
}
