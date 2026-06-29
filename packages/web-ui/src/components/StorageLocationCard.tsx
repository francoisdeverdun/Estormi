/**
 * StorageLocationCard — the single root "storage location" for the whole
 * library (estormi.db + Qdrant + models + logs + distill). Lives in the
 * Maintenance → Storage sub-section, under the StorageBar (which shows the
 * footprint + free space). The selector shows the current path; a native
 * Finder picker chooses a new one and "Move library" queues the move.
 *
 * Changing it does not move anything immediately: the server queues the move
 * (a marker) and Estormi relocates the library — copy → verify → swap — the
 * next time it starts, keeping the old copy as a backup. So a save shows a
 * "reopen to finish" notice. The iCloud vault is intentionally out of scope.
 */
import { useState } from 'react'
import { GhostAction, PrimaryAction, TextInput } from '@estormi/ui-kit'
import { relocateStorage, type StorageLocation } from '../api/storage'
import { pickFolder } from '../api/sources_ext'
import { Modal } from './Modal'
import { Mono } from './Mono'

const MOVE_HINT =
  'Moves the whole library (memory, vectors, models, logs) to the chosen folder ' +
  'on the next launch. The iCloud vault is unaffected.'

export function StorageLocationCard({
  loc,
  onRelocated,
}: {
  loc: StorageLocation | null
  onRelocated: () => void
}) {
  // Empty until the user picks a new folder; the field falls back to the
  // current location so the selector always shows where the library lives.
  const [draft, setDraft] = useState('')
  const [busy, setBusy] = useState(false)
  const [notice, setNotice] = useState('')
  const [confirming, setConfirming] = useState(false)

  const onPick = async () => {
    try {
      const result = await pickFolder('Choose a new storage location for Estormi')
      if (result?.path) {
        setDraft(result.path)
        setNotice('')
      }
    } catch (e) {
      setNotice(e instanceof Error ? e.message : 'Could not open the folder picker.')
    }
  }

  const onMove = async () => {
    const to = draft.trim()
    if (!to) return
    setBusy(true)
    setNotice('')
    try {
      const res = await relocateStorage(to)
      if (res?.ok) {
        setNotice(`Quit and reopen Estormi to move your library to ${res.to}.`)
        setDraft('')
        onRelocated()
      } else {
        setNotice('Could not queue the move.')
      }
    } catch (e) {
      setNotice(e instanceof Error ? e.message : 'Could not queue the move.')
    } finally {
      setBusy(false)
    }
  }

  if (!loc) return null

  const shown = draft || loc.dir
  const canMove = !busy && !!draft.trim() && draft.trim() !== loc.dir

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 6, marginTop: 4 }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
        <TextInput
          type="text"
          value={shown}
          readOnly
          title={shown}
          aria-label="Storage location"
          style={{ flex: '1 1 220px', minWidth: 160, fontFamily: 'var(--font-mono)' }}
        />
        <GhostAction label="Choose folder…" size="sm" onClick={() => void onPick()} disabled={busy} />
        {/* PrimaryAction has a closed prop set (no title passthrough), so wrap
            it to carry the hover explanation — same pattern as DistillationCard. */}
        <span title={MOVE_HINT}>
          <PrimaryAction
            label="Move library"
            size="sm"
            onClick={() => setConfirming(true)}
            disabled={!canMove}
          />
        </span>
      </div>
      {loc.pending && !notice && (
        <Mono dim>↻ queued — reopen Estormi to move the library to {loc.pending}.</Mono>
      )}
      {notice && <Mono dim>{notice}</Mono>}
      <Modal
        open={confirming}
        title="Move library — restart required"
        body={
          `Estormi will move your whole library to “${draft}” the next time it starts. ` +
          `You must quit and reopen the app for the move to take effect — the current ` +
          `copy is kept as a backup until then.`
        }
        confirmLabel="Queue move"
        cancelLabel="Cancel"
        destructive={false}
        onConfirm={() => {
          setConfirming(false)
          void onMove()
        }}
        onCancel={() => setConfirming(false)}
      />
    </div>
  )
}
