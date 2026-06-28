import { expect, test } from '@playwright/test'
import { stubBackend } from './stubs'

/**
 * User-flow e2e for the one-pager, served by ``vite preview`` with a fully
 * stubbed backend (see ``stubs.ts``). Covers: the app boots past its splash
 * and renders the cardinal section, the chunk total from the overview fixture
 * is shown, and clicking the Briefings counter opens the briefing modal.
 */

test.beforeEach(async ({ page }) => {
  await stubBackend(page)
})

test('boots past the splash and renders the cardinal section', async ({ page }) => {
  await page.goto('/')

  // The Summarium wordmark in the cardinal section proves the splash dropped
  // and the real tree mounted.
  await expect(page.getByRole('heading', { name: /ummarium/ })).toBeVisible({
    timeout: 15_000,
  })
  // The "Ars Memoriae" eyebrow appears more than once on the page; assert at
  // least one instance is shown rather than relying on strict-mode uniqueness.
  await expect(page.getByText('Ars Memoriae').first()).toBeVisible()
  // The Briefings counter button is part of the cardinal section.
  await expect(page.getByRole('button', { name: /Briefings/ })).toBeVisible()
})

test('shows the chunk total from the stubbed overview', async ({ page }) => {
  await page.goto('/')

  // total_chunks: 1234 → formatted "1,234".
  await expect(page.getByText('1,234')).toBeVisible({ timeout: 15_000 })
})

test('opening the Briefings counter shows the briefing modal', async ({ page }) => {
  await page.goto('/')

  const counter = page.getByRole('button', { name: /Briefings/ })
  await expect(counter).toBeVisible({ timeout: 15_000 })
  await counter.click()

  // The stubbed briefing body renders inside the modal.
  await expect(page.getByText('Stubbed briefing body for the e2e flow.')).toBeVisible({
    timeout: 10_000,
  })
})
