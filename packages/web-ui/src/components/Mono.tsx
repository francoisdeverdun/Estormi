/**
 * Mono — a small monospace status line (JetBrains Mono, 12px). The shared
 * primitive behind the per-card status text in the model, distillation, and
 * storage-location cards and the MaintenanceCard. ``Hint`` is the dimmed
 * variant (``<Mono dim>``).
 */
import type { ReactNode } from 'react'

export function Mono({ children, dim = false }: { children: ReactNode; dim?: boolean }) {
  return (
    <div
      style={{
        fontFamily: 'var(--font-mono)',
        fontSize: 12,
        color: dim ? 'var(--ink-dim)' : 'var(--ink)',
      }}
    >
      {children}
    </div>
  )
}

export function Hint({ children }: { children: ReactNode }) {
  return <Mono dim>{children}</Mono>
}
