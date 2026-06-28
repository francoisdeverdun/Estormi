/**
 * PathPanel — folder-root picker for path-based sources (documents, code).
 * Extracted from SourceManageModal.tsx.
 */
import { useEffect, useState } from 'react'
import { GhostAction, TextInput } from '@estormi/ui-kit'
import { Eyebrow } from './shared'
import { pickFolder } from '../../api/sources_ext'
import { getSettings, updateSettings } from '../../api/settings'
import type { SourceRowDescriptor } from '../SourceRow'

// Settings key the BACKEND reads for the folder root (see
// `estormi_server/server/sources.py` + `estormi_server/server/jobs.py` — they
// export `${key}_root` as `${KEY}_ROOT` for the ingest script). The
// modal historically wrote `${desc.key}_path`, which was a dead key
// nothing on the server consumed — so the "Pick folder" affordance
// silently did nothing and the code ingester fell back to its default
// (`~/src`). Always write the canonical `_root` key here.
const PATH_SETTING_KEY = (key: string) => `${key}_root`

export function PathPanel({
  desc,
  onChanged,
}: {
  desc: SourceRowDescriptor
  onChanged?: () => void
}) {
  const [path, setPath] = useState(desc.path ?? '')
  const [err, setErr] = useState<string | null>(null)

  // Hydrate from the live settings on mount so users see the value they
  // actually picked previously (instead of the "/Users/you/..." placeholder
  // when there's nothing yet).
  useEffect(() => {
    let cancelled = false
    getSettings()
      .then((s) => {
        if (cancelled) return
        const saved = s[PATH_SETTING_KEY(desc.key)] ?? ''
        if (saved) setPath(saved)
      })
      .catch(() => {
        /* non-fatal — keep whatever the prop seeded */
      })
    return () => {
      cancelled = true
    }
  }, [desc.key])

  const onPick = async () => {
    try {
      const result = await pickFolder(`Select root for ${desc.label}`)
      const picked = result?.path ?? null
      if (picked) {
        setPath(picked)
        await updateSettings({ [PATH_SETTING_KEY(desc.key)]: picked })
        onChanged?.()
      }
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e))
    }
  }

  return (
    <>
      <Eyebrow text={"Root path"} />
      <div style={{ marginBottom: 18 }}>
        <div style={{ display: 'flex', gap: 6 }}>
          <TextInput
            value={path}
            readOnly
            placeholder={desc.path ?? '~/Documents/...'}
            style={{ flex: 1, fontFamily: 'var(--font-mono)' }}
          />
          <GhostAction label={"Pick folder"} size="sm" onClick={() => void onPick()} />
        </div>
        <div
          style={{
            fontFamily: 'var(--font-ui)',
            fontSize: 13,
            color: 'var(--ink-dim)',
            marginTop: 6,
          }}
        >
          Estormi walks the tree below this path on each run.
          {desc.key === 'code' && (
            <>
              {' '}
              Source files become searchable chunks and a structural graph
              (symbols, calls, imports).
            </>
          )}
        </div>
        {err && (
          <div
            style={{
              marginTop: 8,
              color: 'var(--rouge-clair)',
              fontFamily: 'var(--font-mono)',
              fontSize: 13,
            }}
          >
            {err}
          </div>
        )}
      </div>
    </>
  )
}

// iMessage is the one auto-ingesting source that can hard-fail on a macOS
// permission: it needs Full Disk Access to read chat.db, and macOS offers no
// API to *request* FDA — the user must grant it by hand in System Settings.
//
// This is a guided three-state flow rather than a bare error:
//   missing  → explain + deep-link straight to the Full Disk Access pane
//   checking → re-probing (fired automatically on window focus, i.e. when the
//              user returns from System Settings, and via "Re-check now")
//   relaunch → still denied after a check: offer the guaranteed fallback of
//              quitting and reopening Estormi
//
// The re-check (recheckFda) asks the FDA-holding main binary server-side to
// re-snapshot chat.db, so a grant the user just toggled is usually picked up
// live — no relaunch needed. On success we refresh overview via onChanged,
// which flips this source's connection to "connected" and unmounts this panel.
