/**
 * ParametersSection — all configuration on one merged stack.
 *
 * Two sub-panels stacked with tight gaps, no outer wrapper, so they read as
 * one continuous parameters surface in the narrow column:
 *
 *   1. SourcesPanel          — ingestion sources (Notes, Mail, iMessage…,
 *      plus External knowledge whose Manage modal hosts the briefing feeds).
 *      Per-source permission issues surface in each source's Manage modal (e.g.
 *      the iMessage Full Disk Access onboarding), where they're actionable.
 *   2. MaintenanceCard       — storage, model picker, model catalog
 *
 * Each sub-panel renders its own GildedPanel + header inside. We deliberately
 * stack them without an outer container so the surface reads as one merged
 * section, not a section-of-sections.
 */
import { useCallback, useEffect } from 'react'
import { SourcesPanel } from '../components/SourcesPanel'
import { MaintenanceCard } from './MaintenanceCard'
import { getOverview, type Overview } from '../api/overview'
import { useSnapshotState } from '../state/snapshotCache'

export function ParametersSection() {
  const [overview, setOverview] = useSnapshotState<Overview | null>('overview', null)

  const refreshOverview = useCallback(async () => {
    try {
      setOverview(await getOverview())
    } catch {
      /* silent — the cardinal section's poll loop will retry */
    }
  }, [setOverview])

  useEffect(() => {
    void refreshOverview()
  }, [refreshOverview])

  return (
    <div
      style={{
        display: 'flex',
        flexDirection: 'column',
        gap: 12,
      }}
      aria-label="Memoria"
    >
      <SourcesPanel overview={overview} refreshOverview={refreshOverview} />
      <MaintenanceCard />
    </div>
  )
}
