import { useCallback, useEffect, useState } from 'react'
import { getSettings, updateSettings, type Settings } from '../api/settings'

export function useSettings() {
  const [settings, setSettings] = useState<Settings | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  const refresh = useCallback(async () => {
    setLoading(true)
    try {
      setSettings(await getSettings())
      setError(null)
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to load settings')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    void refresh()
  }, [refresh])

  const save = useCallback(async (patch: Settings) => {
    const next = await updateSettings(patch)
    setSettings((prev) => ({ ...(prev ?? {}), ...(next ?? patch) }))
    return next
  }, [])

  return { settings, loading, error, refresh, save }
}
