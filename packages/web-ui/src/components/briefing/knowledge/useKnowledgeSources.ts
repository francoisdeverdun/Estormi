/**
 * useKnowledgeSources — the load / add / update / remove state behind the
 * Diarium panel, backed by GET/PUT /api/knowledge/sources. Optimistically
 * mutates the local list then persists; on failure it surfaces the error and
 * reloads from the server. Extracted from KnowledgeSourcesPanel.tsx.
 */
import { useEffect, useState } from 'react'
import {
  getKnowledgeSources,
  putKnowledgeSources,
  type KnowledgeSource,
} from '../../../api/settings'

export function useKnowledgeSources() {
  const [sources, setSources] = useState<KnowledgeSource[] | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState<boolean>(true)
  const [savedFlash, setSavedFlash] = useState<boolean>(false)
  const [expandedIdx, setExpandedIdx] = useState<number | null>(null)

  const load = async () => {
    try {
      const list = await getKnowledgeSources()
      setSources(list)
      setError(null)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    void load()
  }, [])

  function flash() {
    setSavedFlash(true)
    window.setTimeout(() => setSavedFlash(false), 1500)
  }

  const removeRow = async (idx: number) => {
    if (!sources) return
    const next = sources.filter((_, i) => i !== idx)
    setSources(next)
    setExpandedIdx((cur) => {
      if (cur === null) return cur
      if (cur === idx) return null
      return cur > idx ? cur - 1 : cur
    })
    try {
      await putKnowledgeSources(next)
      flash()
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
      void load()
    }
  }

  const updateRow = async (idx: number, patch: KnowledgeSource) => {
    if (!sources) return
    const next = sources.map((s, i) => (i === idx ? patch : s))
    setSources(next)
    setExpandedIdx(null)
    try {
      await putKnowledgeSources(next)
      flash()
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
      void load()
    }
  }

  const addRow = async (src: KnowledgeSource) => {
    const next = [...(sources ?? []), src]
    setSources(next)
    try {
      await putKnowledgeSources(next)
      flash()
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
      void load()
    }
  }

  return {
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
  }
}
