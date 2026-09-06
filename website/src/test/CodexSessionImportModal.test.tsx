import { beforeEach, describe, expect, it, vi } from 'vitest'
import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'

import CodexSessionImportModal from '../components/CodexSessionImportModal'
import { api } from '../api/client'
import { renderWithProviders } from './helpers'

const THREAD = {
  id: '019abcdef-1234-7890-abcd-1234567890ab',
  title: 'Fix the dashboard',
  preview: 'Investigate the session picker',
  cwd: '/workspace/project',
  created_at: 1_700_000_000,
  updated_at: 1_700_000_100,
  source: 'cli',
  imported: false,
  local_session: '',
}

describe('CodexSessionImportModal', () => {
  beforeEach(() => {
    vi.restoreAllMocks()
    vi.spyOn(api, 'codexThreads').mockResolvedValue({ threads: [THREAD] })
  })

  it('imports a native fork by default', async () => {
    const imported = vi.spyOn(api, 'importCodexThread').mockResolvedValue({
      key: 'chat-1-import',
      title: THREAD.title,
      codex_thread_id: '019forked',
      import_mode: 'fork',
      imported_messages: 2,
    })
    const onImported = vi.fn()
    const onClose = vi.fn()
    renderWithProviders(
      <CodexSessionImportModal open onClose={onClose} onImported={onImported} />,
    )

    expect(await screen.findByText('Fix the dashboard')).toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: 'Import as new' }))

    await waitFor(() => expect(imported).toHaveBeenCalledWith(THREAD.id, 'fork'))
    await waitFor(() => expect(onImported).toHaveBeenCalledWith(expect.objectContaining({ key: 'chat-1-import' })))
    expect(onClose).toHaveBeenCalled()
  })

  it('can deliberately continue the original thread', async () => {
    const imported = vi.spyOn(api, 'importCodexThread').mockResolvedValue({
      key: 'chat-2-import',
      codex_thread_id: THREAD.id,
      import_mode: 'resume',
      imported_messages: 2,
    })
    renderWithProviders(
      <CodexSessionImportModal open onClose={vi.fn()} onImported={vi.fn()} />,
    )

    await userEvent.click(await screen.findByRole('button', { name: 'Continue original' }))

    await waitFor(() => expect(imported).toHaveBeenCalledWith(THREAD.id, 'resume'))
  })
})
