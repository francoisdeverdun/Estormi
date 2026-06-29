/**
 * ResetButton — destructive reset affordance gated by a confirm Modal.
 *
 * Encapsulates the click → confirm → run → flash success flow used by the
 * Briefing modal to reset its engine's composed state. The reset API is
 * passed in; this component knows nothing about which surface it's resetting.
 */
import { useState } from 'react'
import { GhostAction } from '@estormi/ui-kit'
import { Modal } from './Modal'

export interface ResetButtonProps {
  /** Label on the trigger button (e.g. "Reset briefings"). */
  label: string
  /** Title of the confirm dialog. */
  confirmTitle: string
  /** Body copy in the confirm dialog. */
  confirmBody: string
  /** The actual reset call — anything returning a promise. */
  onReset: () => Promise<unknown>
  /** Optional callback once the reset has succeeded (parent can refresh). */
  onDone?: () => void
}

export function ResetButton({
  label,
  confirmTitle,
  confirmBody,
  onReset,
  onDone,
}: ResetButtonProps) {
  const [confirming, setConfirming] = useState(false)
  const [busy, setBusy] = useState(false)
  const [flash, setFlash] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const run = async () => {
    setConfirming(false)
    setBusy(true)
    setError(null)
    try {
      await onReset()
      setFlash(true)
      window.setTimeout(() => setFlash(false), 3500)
      onDone?.()
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
      window.setTimeout(() => setError(null), 4500)
    } finally {
      setBusy(false)
    }
  }

  return (
    <>
      <GhostAction
        label={
          busy
            ? '…'
            : flash
              ? '✓ Reset'
              : error
                ? '! Failed'
                : label
        }
        size="sm"
        onClick={() => setConfirming(true)}
        disabled={busy}
      />
      {confirming && (
        <Modal
          open
          title={confirmTitle}
          body={confirmBody}
          confirmLabel="Reset"
          cancelLabel="Cancel"
          destructive
          onCancel={() => setConfirming(false)}
          onConfirm={() => void run()}
        />
      )}
    </>
  )
}
