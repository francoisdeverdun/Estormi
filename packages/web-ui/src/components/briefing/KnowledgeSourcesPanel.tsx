/**
 * KnowledgeSourcesPanel — Diarium.
 *
 * "Diarium" (Latin: a daily journal) configures the *sources* the daily
 * briefing draws from: the YouTube / RSS feed list (add / edit / remove).
 * How the briefing is *built* from those sources — schedule, output language
 * and reasoning backend — lives separately in Officina (the MaintenanceCard),
 * because ingestion of sources and composition of the briefing are two
 * distinct processes.
 *
 * Backend: GET/PUT /api/knowledge/sources. The list state + CRUD lives in
 * useKnowledgeSources; the row and the add/edit forms live under `knowledge/`.
 */
import { useState } from 'react'
import {
  EmptyState,
  ErrorState,
  GhostAction,
  LoadingState,
} from '@estormi/ui-kit'
import { useKnowledgeSources } from './knowledge/useKnowledgeSources'
import { KnowledgeRow } from './knowledge/KnowledgeRow'
import { AddSourceForm } from './knowledge/AddSourceForm'

/**
 * Renders the source feed list (add / edit / remove). It always lives inside
 * the External knowledge source's Manage modal, which already supplies the
 * gilt frame and the "Diarium" title, so the panel draws no chrome of its own.
 */
export function KnowledgeSourcesPanel() {
  const {
    sources,
    error,
    loading,
    savedFlash,
    expandedIdx,
    setExpandedIdx,
    load,
    removeRow,
    updateRow,
    addRow,
  } = useKnowledgeSources()
  const [adding, setAdding] = useState<boolean>(false)

  return (
    <div>
      {/* The host Manage modal already shows the "Diarium" title, so only the
          Add/Saved controls surface here (right-aligned). */}
      <div style={{ marginBottom: 12 }}>
        <div
          style={{
            display: 'flex',
            alignItems: 'center',
            gap: 8,
            flexWrap: 'wrap',
            rowGap: 6,
          }}
        >
          <div
            style={{
              display: 'flex',
              gap: 8,
              alignItems: 'center',
              marginLeft: 'auto',
            }}
          >
            {savedFlash && (
              <span
                style={{
                  fontFamily: 'var(--font-display)',
                  fontSize: 12,
                  letterSpacing: '0.22em',
                  color: 'var(--vert-sauge)',
                  textTransform: 'uppercase',
                }}
              >
                {"Saved"}
              </span>
            )}
            <GhostAction
              label={adding ? 'Close' : 'Add source'}
              active={adding}
              onClick={() => setAdding((v) => !v)}
            />
          </div>
        </div>
      </div>
      {adding && (
        <AddSourceForm
          existingIds={
            (sources ?? [])
              .map((s) => s.id)
              .filter((id): id is string => Boolean(id))
          }
          onAdd={(s) => {
            setAdding(false)
            void addRow(s)
          }}
          onCancel={() => setAdding(false)}
        />
      )}
      {error ? (
        <ErrorState
          message={"Could not load knowledge sources"}
          detail={error}
          onRetry={load}
        />
      ) : loading && !sources ? (
        <LoadingState label={"Reading sources"} />
      ) : !sources || sources.length === 0 ? (
        <EmptyState
          title={"No knowledge sources yet"}
          body={"Add source"}
        />
      ) : (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 2 }}>
          {sources.map((s, i) => (
            <KnowledgeRow
              key={s.id ?? `${s.label ?? 'src'}-${i}`}
              source={s}
              expanded={expandedIdx === i}
              onToggle={() =>
                setExpandedIdx((cur) => (cur === i ? null : i))
              }
              onToggleEnabled={() =>
                void updateRow(i, { ...s, enabled: !(s.enabled !== false) })
              }
              onRemove={() => void removeRow(i)}
              onSave={(patch) => void updateRow(i, patch)}
              onCancel={() => setExpandedIdx(null)}
            />
          ))}
        </div>
      )}
    </div>
  )
}
