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
  // CI runners render/transition slower than local: some assertions (e.g. the
  // Engine room popover opening) exceed the 5s default there while passing
  // locally. A 15s global expect timeout matches the per-assertion overrides
  // already sprinkled through the suite, without the 60s-per-test overkill.
  expect: { timeout: 15_000 },
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
    command: 'pnpm build && pnpm preview --port 4173 --strictPort --host 127.0.0.1',
    url: 'http://127.0.0.1:4173',
    reuseExistingServer: !process.env.CI,
    // Pin the preview host to 127.0.0.1: vite preview otherwise binds to
    // localhost/IPv6 (::1), so Playwright's IPv4 readiness probe on the url
    // above never connects and hangs until timeout. Headroom covers the cold
    // `pnpm build` that shares this single window.
    timeout: 180_000,
  },
})
