import { defineConfig, devices } from '@playwright/test'

/**
 * Playwright config for the Estormi one-pager SPA.
 *
 * Hermeticity strategy: we build the SPA and serve it with ``vite preview``
 * (a static server, no backend). Every ``/api/**`` + ``/health`` request is
 * stubbed inside each test via ``page.route`` (see ``e2e/stubs.ts``), so the
 * tests never depend on a running FastAPI server. The preview server boots
 * once and is reused across the run.
 */
export default defineConfig({
  testDir: './e2e',
  fullyParallel: true,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 1 : 0,
  reporter: process.env.CI ? 'github' : 'list',
  use: {
    baseURL: 'http://127.0.0.1:4173',
    trace: 'on-first-retry',
  },
  projects: [
    {
      name: 'chromium',
      use: { ...devices['Desktop Chrome'] },
    },
  ],
  webServer: {
    // Build then serve the static bundle. `--strictPort` makes a port clash
    // fail loudly instead of silently picking another port the tests can't
    // reach.
    command: 'pnpm build && pnpm preview --port 4173 --strictPort',
    url: 'http://127.0.0.1:4173',
    reuseExistingServer: !process.env.CI,
    timeout: 120_000,
  },
})
