import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

/**
 * Vite config for the Estormi one-pager SPA (@estormi/web-ui).
 *
 *  - dev proxies /api, /health, /docs, /source-icons, /brand, /fonts to FastAPI on :8000
 *  - `base: './'` keeps bundled asset URLs relative so the SPA can mount
 *    under any prefix the host chooses (FastAPI's `/app` mount, or Tauri's
 *    bundled `tauri://` scheme)
 *  - dev port :5175
 */
export default defineConfig({
  plugins: [react()],
  base: './',
  server: {
    port: 5175,
    proxy: {
      '/api': 'http://127.0.0.1:8000',
      '/health': 'http://127.0.0.1:8000',
      '/docs': 'http://127.0.0.1:8000',
      '/source-icons': 'http://127.0.0.1:8000',
      '/brand': 'http://127.0.0.1:8000',
      '/fonts': 'http://127.0.0.1:8000',
    },
  },
  build: {
    outDir: 'dist',
    emptyOutDir: true,
    // No source maps in the production bundle — they added ~947 KB of .map
    // files to the Tauri resource payload for no shipped benefit. Switch to
    // 'hidden' locally if you need to debug a built artifact.
    sourcemap: false,
  },
})
