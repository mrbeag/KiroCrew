import { fireEvent, screen, waitFor } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

import type { CodexUsagePayload } from '../api/client'
import CodexUsageModal from '../components/CodexUsageModal'
import { renderWithProviders } from './helpers'

const USAGE: CodexUsagePayload = {
  available: true,
  plan: 'plus',
  limit_name: 'codex_standard',
  primary: { used_percent: 12, window_minutes: 300, resets_at: 1_800_000_000 },
  secondary: { used_percent: 43, window_minutes: 10_080, resets_at: 1_800_086_400 },
  credits: { hasCredits: true, unlimited: false, balance: '25.00' },
}

describe('CodexUsageModal', () => {
  it('shows plan, both rolling windows, reset values, and credits', async () => {
    renderWithProviders(<CodexUsageModal open onClose={vi.fn()} usage={USAGE} />)

    expect(await screen.findByRole('dialog', { name: 'Codex usage' })).toBeInTheDocument()
    expect(screen.getByText('Plus')).toBeInTheDocument()
    expect(screen.getByText('Codex Standard')).toBeInTheDocument()
    expect(screen.getByText('25.00')).toBeInTheDocument()
    expect(screen.getByText('5-hour window')).toBeInTheDocument()
    expect(screen.getByText('Weekly window')).toBeInTheDocument()
    expect(screen.getByText('88% remaining')).toBeInTheDocument()
    expect(screen.getByText('57% remaining')).toBeInTheDocument()
    expect(screen.queryByText('12% used')).not.toBeInTheDocument()
    expect(screen.queryByText('43% used')).not.toBeInTheDocument()
    expect(screen.getByRole('progressbar', { name: '5-hour window remaining' })).toHaveAttribute('aria-valuenow', '88')
    expect(screen.getByRole('progressbar', { name: 'Weekly window remaining' })).toHaveAttribute('aria-valuenow', '57')
  })

  it('distinguishes loading, failure, and an available payload without windows', async () => {
    const view = renderWithProviders(<CodexUsageModal open onClose={vi.fn()} usage={null} />)
    expect(await screen.findByText('Checking Codex usage…')).toBeInTheDocument()

    view.rerender(<CodexUsageModal open onClose={vi.fn()} usage="failed" />)
    expect(await screen.findByText('Codex usage is unavailable.')).toBeInTheDocument()

    view.rerender(<CodexUsageModal open onClose={vi.fn()} usage={{ available: true }} />)
    expect(await screen.findByText('No rolling-window values were reported.')).toBeInTheDocument()
  })

  it('closes through the accessible close control', async () => {
    const onClose = vi.fn()
    renderWithProviders(<CodexUsageModal open onClose={onClose} usage={USAGE} />)

    fireEvent.click(await screen.findByRole('button', { name: 'Close' }))
    await waitFor(() => expect(onClose).toHaveBeenCalledOnce())
  })
})
