/**
 * Modal — confirmation dialog in the Ars Memoriae idiom.
 *
 * A small overlay used for destructive confirmations (Admin resets on the
 * Settings page). It owns no state of its own: parents render it conditionally
 * when `open` is true. The visual language matches the canonical card:
 *   - charbon body sitting on a near-opaque encre scrim,
 *   - a pourpre frame when the action is destructive,
 *   - a gilt-line border, Fleurons flanking the title,
 *   - Cinzel/EB Garamond typography only,
 *   - cancel = GhostAction; confirm = red-contour GhostAction when destructive
 *     (the default), filled-gold PrimaryAction for an affirmative dialog.
 *
 * The scrim + dialog mechanics (role, aria, scrim-click close, focus-on-open,
 * stacked Escape) live in the shared ``ModalOverlay``; this component supplies
 * the confirm-dialog look and the title/body/buttons. Escape and scrim click
 * both invoke ``onCancel``; the stacked-Escape policy dismisses one layer at a
 * time when a confirm is opened over another modal.
 */
import { Fleuron } from '@estormi/ui-kit'
import { PrimaryAction, GhostAction } from '@estormi/ui-kit'
import { ModalOverlay } from './ModalOverlay'

export interface ModalProps {
  open: boolean
  title: string
  body?: React.ReactNode
  confirmLabel?: string
  cancelLabel?: string
  destructive?: boolean
  onConfirm: () => void
  onCancel: () => void
}

export function Modal({
  open,
  title,
  body,
  confirmLabel,
  cancelLabel,
  destructive = true,
  onConfirm,
  onCancel,
}: ModalProps) {
  const confirmText = confirmLabel ?? 'Confirm'
  const cancelText = cancelLabel ?? 'Cancel'

  if (!open) return null

  return (
    <ModalOverlay
      onClose={onCancel}
      closeOnScrim="click"
      escape="lifo-stack"
      focusOnOpen
      scrimBackground="var(--scrim-modal-soft)"
      labelledBy="estormi-modal-title"
      dialogStyle={{
        width: '100%',
        maxWidth: 480,
        background: 'var(--charbon)',
        // Destructive intent moves from the old top accent bar into the frame
        // colour — the rounded iOS-style panel keeps one uniform hairline.
        border: `1px solid ${destructive ? 'var(--pourpre)' : 'var(--gilt-line-strong)'}`,
        borderRadius: 'var(--radius-panel)',
        padding: '24px 26px 22px',
        position: 'relative',
        boxShadow: '0 18px 60px var(--shadow-soft)',
      }}
    >
      <div
        style={{
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'center',
          gap: 10,
          marginBottom: 14,
        }}
      >
        <Fleuron size={9} color="var(--or-ancien)" />
        <h2
          id="estormi-modal-title"
          style={{
            fontFamily: 'var(--font-display)',
            fontSize: 17,
            letterSpacing: '0.24em',
            textTransform: 'uppercase',
            color: destructive ? 'var(--pourpre-clair)' : 'var(--or-clair)',
            margin: 0,
            textAlign: 'center',
            fontWeight: 600,
          }}
        >
          {title}
        </h2>
        <Fleuron size={9} color="var(--or-ancien)" />
      </div>

      {body && (
        <div
          style={{
            fontFamily: 'var(--font-body)',
            fontSize: 18,
            lineHeight: 1.55,
            color: 'var(--parchemin)',
            textAlign: 'center',
            padding: '4px 4px 22px',
          }}
        >
          {body}
        </div>
      )}

      <div
        style={{
          display: 'flex',
          justifyContent: 'center',
          gap: 12,
          paddingTop: 6,
          borderTop: '1px solid var(--gilt-line)',
          marginTop: 4,
        }}
      >
        <GhostAction label={cancelText} onClick={onCancel} />
        {destructive ? (
          <GhostAction label={confirmText} tone="danger" onClick={onConfirm} />
        ) : (
          <PrimaryAction label={confirmText} onClick={onConfirm} />
        )}
      </div>
    </ModalOverlay>
  )
}
