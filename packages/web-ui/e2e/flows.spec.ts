import { expect, test } from '@playwright/test'
import { stubBackend } from './stubs'

/**
 * Key-flow e2e for the one-pager, complementing ``app.spec.ts`` (boot +
 * briefing modal). Covers the interactive surfaces a user actually drives:
 *
 *   1. Sources management — the Memoria source list renders rows and the ⋮
 *      control opens the per-source Manage modal.
 *   2. Engine room — the top-bar engine pulse opens the Engine room popover.
 *   3. Error states — a backend that 500s on the briefings list surfaces the
 *      ErrorState panel instead of hanging or rendering a blank modal.
 *
 * All backend traffic is stubbed (see ``stubs.ts``); no FastAPI server runs.
 */

test.beforeEach(async ({ page }) => {
  await stubBackend(page)
})

// ── 1. Sources management ───────────────────────────────────────────────────
test('renders the Memoria source rows from the overview', async ({ page }) => {
  await page.goto('/')

  // The notes + mail rows come from the stubbed overview source counts.
  const notesRow = page.locator('[data-source="notes"]')
  await expect(notesRow).toBeVisible({ timeout: 15_000 })
  await expect(page.locator('[data-source="mail"]')).toBeVisible()
})

test('the ⋮ control opens the per-source Manage modal', async ({ page }) => {
  await page.goto('/')

  const manage = page.getByRole('button', { name: 'Manage source · Apple Notes' })
  await expect(manage).toBeVisible({ timeout: 15_000 })
  await manage.click()

  const dialog = page.getByRole('dialog')
  await expect(dialog).toBeVisible()
  // The header carries the canonical Manage eyebrow + the source name.
  await expect(dialog.getByText('Source · Manage')).toBeVisible()
  await expect(dialog.getByRole('heading', { name: /Apple Notes/ })).toBeVisible()

  // Escape closes it.
  await page.keyboard.press('Escape')
  await expect(dialog).not.toBeVisible()
})

// ── 2. Engine room ──────────────────────────────────────────────────────────
test('the engine pulse opens the Engine room popover', async ({ page }) => {
  await page.goto('/')

  const pulse = page.locator('button[aria-haspopup="dialog"]').first()
  await expect(pulse).toBeVisible({ timeout: 15_000 })
  await expect(pulse).toHaveAttribute('aria-expanded', 'false')
  await pulse.click()

  const popover = page.getByRole('dialog', { name: 'Engine room' })
  await expect(popover).toBeVisible()
  await expect(pulse).toHaveAttribute('aria-expanded', 'true')
})

// ── 3. Error states ─────────────────────────────────────────────────────────
test('a failing briefings list surfaces the error panel, not a blank modal', async ({
  page,
}) => {
  // Override the briefings list to 500 (registered after stubBackend, so it
  // wins). The modal must render the ErrorState rather than spin forever.
  await page.route('**/api/briefings', (r) =>
    r.fulfill({ status: 500, contentType: 'application/json', body: '{}' }),
  )

  await page.goto('/')

  const counter = page.getByRole('button', { name: /Briefings/ })
  await expect(counter).toBeVisible({ timeout: 15_000 })
  await counter.click()

  await expect(page.getByText('Could not load briefings')).toBeVisible({
    timeout: 10_000,
  })
})
