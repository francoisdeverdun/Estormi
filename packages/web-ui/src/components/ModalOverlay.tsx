/**
 * ModalOverlay — the shared low-level scrim+dialog scaffold every web-ui modal
 * builds on. It owns ONLY the mechanics that were copy-pasted across five
 * hand-rolled modals; each caller keeps its own look (via `dialogStyle`) and its
 * own body (`children`) and selects a policy through props:
 *
 *   - scrim dismissal — `closeOnScrim`:
 *       'click'      → a plain click on the scrim closes (confirm dialogs).
 *       'drag-guard' → closes only when BOTH mousedown and mouseup land on the
 *                      scrim, so a drag that starts inside the dialog (e.g.
 *                      selecting log text) and ends on the scrim does NOT close.
 *   - Escape — `escape`:
 *       'plain'      → Escape closes.
 *       'lifo-stack' → Escape closes only the TOPMOST overlay, so a stacked
 *                      confirm dismisses one layer at a time instead of
 *                      collapsing every layer at once.
 *   - `lockBodyScroll`, `portal`, `focusOnOpen`, `zIndex`, `scrimBackground`,
 *     `scrimBackdropFilter`, `ariaLabel`/`labelledBy`.
 *
 * The overlay is mounted only while visible — callers gate their own `open`
 * state and render this once they decide to show, so the effects below run on
 * open and clean up on close.
 */
import { useEffect, useRef } from 'react'
import type { CSSProperties, MouseEvent as ReactMouseEvent, ReactNode } from 'react'
import { createPortal } from 'react-dom'

/**
 * LIFO stack of open `escape="lifo-stack"` overlays. Each pushes a token on
 * mount; the shared keydown handler closes only the topmost. `stopPropagation`
 * can't do this because every overlay attaches its listener to the *same*
 * window target — so without the gate one Escape would cancel every stacked
 * layer at once.
 */
const overlayStack: symbol[] = []

export interface ModalOverlayProps {
  onClose: () => void
  closeOnScrim?: 'click' | 'drag-guard'
  escape?: 'plain' | 'lifo-stack'
  lockBodyScroll?: boolean
  /** Render into a portal on document.body — needed to escape an ancestor
   *  stacking/overflow context (e.g. the engine-room popover). */
  portal?: boolean
  /** Move focus into the dialog on open so keyboard users land here. */
  focusOnOpen?: boolean
  zIndex?: number
  /** Scrim background — a CSS value, usually a `--scrim-*` token. */
  scrimBackground: string
  /** Optional scrim backdrop-filter, e.g. 'blur(6px)'. */
  scrimBackdropFilter?: string
  ariaLabel?: string
  /** `aria-labelledby` id; preferred over `ariaLabel` when the dialog renders a
   *  labelled heading. */
  labelledBy?: string
  /** Inline style for the dialog frame — each caller owns its own look. */
  dialogStyle?: CSSProperties
  children: ReactNode
}

export function ModalOverlay({
  onClose,
  closeOnScrim = 'drag-guard',
  escape = 'plain',
  lockBodyScroll = false,
  portal = false,
  focusOnOpen = false,
  zIndex = 200,
  scrimBackground,
  scrimBackdropFilter,
  ariaLabel,
  labelledBy,
  dialogStyle,
  children,
}: ModalOverlayProps) {
  const dialogRef = useRef<HTMLDivElement | null>(null)
  // Latest onClose, so the listener registered once on mount always calls the
  // current handler without re-subscribing — re-subscribing would re-push the
  // LIFO token and reorder the stack on every parent render.
  const onCloseRef = useRef(onClose)
  onCloseRef.current = onClose
  // Drag-guard: did the press start on the scrim itself?
  const scrimDown = useRef(false)

  // Escape handling — registered once on mount.
  useEffect(() => {
    const token = Symbol('overlay')
    if (escape === 'lifo-stack') overlayStack.push(token)
    const handler = (e: KeyboardEvent) => {
      if (e.key !== 'Escape') return
      if (escape === 'lifo-stack' && overlayStack[overlayStack.length - 1] !== token) return
      e.stopPropagation()
      onCloseRef.current()
    }
    window.addEventListener('keydown', handler)
    return () => {
      window.removeEventListener('keydown', handler)
      if (escape === 'lifo-stack') {
        const i = overlayStack.indexOf(token)
        if (i >= 0) overlayStack.splice(i, 1)
      }
    }
  }, [escape])

  // Body scroll lock while open.
  useEffect(() => {
    if (!lockBodyScroll) return
    const prev = document.body.style.overflow
    document.body.style.overflow = 'hidden'
    return () => {
      document.body.style.overflow = prev
    }
  }, [lockBodyScroll])

  // Focus into the dialog on open.
  useEffect(() => {
    if (focusOnOpen) dialogRef.current?.focus()
  }, [focusOnOpen])

  // Scrim close behaviour. 'click' closes on a plain scrim click; 'drag-guard'
  // only closes when the press both starts AND ends on the scrim.
  const scrimHandlers =
    closeOnScrim === 'click'
      ? {
          onClick: (e: ReactMouseEvent) => {
            if (e.target === e.currentTarget) onClose()
          },
        }
      : {
          onMouseDown: (e: ReactMouseEvent) => {
            scrimDown.current = e.target === e.currentTarget
          },
          onMouseUp: (e: ReactMouseEvent) => {
            if (scrimDown.current && e.target === e.currentTarget) onClose()
            scrimDown.current = false
          },
        }

  const node = (
    <div
      role="presentation"
      {...scrimHandlers}
      style={{
        position: 'fixed',
        inset: 0,
        zIndex,
        background: scrimBackground,
        ...(scrimBackdropFilter ? { backdropFilter: scrimBackdropFilter } : {}),
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'center',
        padding: 24,
      }}
    >
      <div
        ref={dialogRef}
        role="dialog"
        aria-modal="true"
        aria-label={labelledBy ? undefined : ariaLabel}
        aria-labelledby={labelledBy}
        tabIndex={-1}
        style={{ outline: 'none', ...dialogStyle }}
      >
        {children}
      </div>
    </div>
  )

  return portal ? createPortal(node, document.body) : node
}
