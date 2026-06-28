/**
 * AppFrame — flat backdrop behind the entire main app. Estormi sits on a
 * clean rectangle; this component takes care of:
 *   - making html/body transparent (Tauri main window has no decorations)
 *   - hiding scrollbar gutters
 *   - suppressing the radial overlay tokens.css adds via body::before
 */
import { useEffect } from 'react'

export function AppFrame() {
  useEffect(() => {
    const prevHtml = document.documentElement.style.background
    const prevBody = document.body.style.background
    document.documentElement.style.background = 'transparent'
    document.body.style.background = 'transparent'
    const styleEl = document.createElement('style')
    styleEl.textContent = `
      body::before { display: none !important; }
      html, body, * { scrollbar-width: none !important; }
      ::-webkit-scrollbar { width: 0 !important; height: 0 !important; display: none !important; }
    `
    document.head.appendChild(styleEl)
    return () => {
      document.documentElement.style.background = prevHtml
      document.body.style.background = prevBody
      styleEl.remove()
    }
  }, [])

  return (
    <div
      aria-hidden="true"
      style={{
        position: 'fixed',
        inset: 0,
        background:
          'radial-gradient(ellipse at 40% 30%, var(--charbon) 0%, var(--encre-mi) 55%, var(--encre) 100%)',
        pointerEvents: 'none',
        zIndex: 0,
      }}
    />
  )
}
