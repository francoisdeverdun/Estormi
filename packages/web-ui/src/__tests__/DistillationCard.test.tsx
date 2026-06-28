/**
 * Tests for ``DistillationCard``.
 *
 * Regression guard: a killed/crashed distill run leaves its in-flight phase
 * (e.g. "train") in status.json with no terminal write. The card must treat
 * "active" by the running engine snapshot, NOT the stale phase string — otherwise
 * the button stays disabled and "Training (QLoRA)…" shows forever.
 */
import { render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { DistillationCard } from '../components/DistillationCard'
import { getDistillStatus } from '../api/distill'
import { getSettings } from '../api/settings'

vi.mock('../api/distill', () => ({
  getDistillStatus: vi.fn(),
  runDistill: vi.fn(),
  deleteDistillTooling: vi.fn(),
  distillToolingInstallPath: () => '/api/distill/tooling/install',
}))
vi.mock('../api/settings', () => ({
  getSettings: vi.fn(),
  updateSettings: vi.fn(),
}))

const STALE_STATUS = {
  status: { phase: 'train', error: '' },
  references: { days: [], count: 19, vaultCount: 19, models: { archive: 16, 'user-edited': 3 } },
  tooling: { python: '', mlx_lm: '', quantize: '', convert: '', ready: true },
  installed: false,
  installedFile: '',
  running: [], // ← nothing is actually running
}

beforeEach(() => {
  vi.mocked(getDistillStatus).mockResolvedValue({ ...STALE_STATUS } as never)
  vi.mocked(getSettings).mockResolvedValue({ distill_schedule_cron: '0 3 * * 0' } as never)
})

describe('<DistillationCard>', () => {
  it('is not stuck "active" when the last phase is stale but no engine is running', async () => {
    render(<DistillationCard />)
    // The run button must be ENABLED — not disabled on a leftover "train" phase.
    await waitFor(() =>
      expect(screen.getByRole('button', { name: /distill my quill/i })).toBeEnabled(),
    )
    // And the live "Training (QLoRA)…" label must NOT show.
    expect(screen.queryByText(/Training \(QLoRA\)…/)).not.toBeInTheDocument()
  })

  it('shows the live phase label only while an engine is actually running', async () => {
    vi.mocked(getDistillStatus).mockResolvedValue({
      ...STALE_STATUS,
      running: [{ kind: 'distill', source: 'manual' }],
    } as never)
    render(<DistillationCard />)
    await waitFor(() =>
      expect(screen.getByText(/Training \(QLoRA\)…/)).toBeInTheDocument(),
    )
  })
})
