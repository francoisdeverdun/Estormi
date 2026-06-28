/**
 * ExtModalShell — large, free-form modal. Used by the Briefing modal.
 *
 * Different from the canonical ``Modal`` (a destructive-confirm dialog with
 * title + body + 2 buttons). This shell is a full-height panel: a top accent
 * line, a pinned Close affordance, and an inner body the caller composes
 * freely. The scrim + dialog mechanics (drag-guard scrim close, Escape,
 * body-scroll lock, focus-on-open) come from the shared ``ModalOverlay``.
 */
import type { ReactNode } from 'react'
import { ModalOverlay } from './ModalOverlay'

export interface ExtModalShellProps {
  onClose: () => void
  /** Max-width in pixels for the shell. */
  maxWidth?: number
  /** Colour for the 3-pixel top accent line. */
  accent?: string
  /** Aria-label for the dialog (used when no labelled heading). */
  ariaLabel?: string
  /** Optional `aria-labelledby` id (preferred over ariaLabel). */
  labelledBy?: string
  children: ReactNode
}

export function ExtModalShell({
  onClose,
  // Same width as the engine-log modal so every shell shares one footprint.
  // The shell fills the available column down to a small side margin (set by
  // the scrim padding); callers rarely need to override this.
  maxWidth = 720,
  accent = 'var(--or-ancien)',
  ariaLabel,
  labelledBy,
  children,
}: ExtModalShellProps) {
  return (
    <ModalOverlay
      onClose={onClose}
      closeOnScrim="drag-guard"
      escape="plain"
      lockBodyScroll
      focusOnOpen
      scrimBackground="var(--scrim-modal)"
      scrimBackdropFilter="blur(6px)"
      ariaLabel={ariaLabel}
      labelledBy={labelledBy}
      dialogStyle={{
        width: '100%',
        maxWidth,
        maxHeight: 'calc(100vh - 48px)',
        background: 'var(--charbon)',
        border: '1px solid var(--gilt-line-strong)',
        borderTop: `3px solid ${accent}`,
        borderRadius: 'var(--radius-panel)',
        boxShadow: '0 20px 80px var(--shadow-modal)',
        display: 'flex',
        flexDirection: 'column',
        overflow: 'hidden',
        position: 'relative',
      }}
    >
      {/* Fixed Close affordance so every modal opened via this shell has a
          visible exit — independent of whatever the child renders as its
          internal header. Pinned top-right with z-index above content so it
          stays clickable even in scrolling panes. */}
      <button
        type="button"
        onClick={onClose}
        aria-label="Close"
        style={{
          position: 'absolute',
          top: 10,
          right: 10,
          zIndex: 5,
          padding: '4px 10px',
          background: 'color-mix(in srgb, var(--encre) 85%, transparent)',
          border: '1px solid var(--gilt-line-strong)',
          borderRadius: 'var(--radius-tight)',
          color: 'var(--ink-dim)',
          fontFamily: 'var(--font-display)',
          fontSize: 10,
          letterSpacing: '0.18em',
          textTransform: 'uppercase',
          cursor: 'pointer',
        }}
      >
        Close
      </button>
      {children}
    </ModalOverlay>
  )
}
