/**
 * Estormi — Ars Memoriae — Vite entry (one-pager).
 *
 * Single-screen layout: a top bar (engine pulse) above one scrolling column
 * of two sections — Cardinal (metrics) and Parameters (sources, knowledge
 * sources, maintenance). No sidebar, no router. Dark-only.
 */
import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import '@estormi/ui-kit/tokens.css'
import '@estormi/ui-kit/briefing.css'
import { installDemoInterceptor } from './demo/demoInterceptor'
import { App } from './App'

document.documentElement.setAttribute('data-theme', 'dark')

// In demo mode, patch fetch before any component mounts.
installDemoInterceptor()

const root = document.getElementById('root')
if (!root) {
  throw new Error('Estormi: missing #root in index.html')
}

createRoot(root).render(
  <StrictMode>
    <App />
  </StrictMode>,
)
