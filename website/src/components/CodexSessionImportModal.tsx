import { useMemo, useState } from 'react'
import { useMutation, useQuery } from '@tanstack/react-query'
import { Copy, Loader2, Play, RefreshCw, Search } from 'lucide-react'

import { api, type CodexThreadImportResult, type CodexThreadSummary } from '../api/client'
import { fmtDateTimeNumeric } from '../i18n/format'
import { i18nT } from '../i18n/t'
import Modal from './Modal'
import ErrorNotice from './ErrorNotice'

interface Props {
  open: boolean
  onClose: () => void
  onImported: (slot: CodexThreadImportResult) => void
}

function threadDate(thread: CodexThreadSummary): string {
  const epoch = thread.updated_at ?? thread.created_at
  if (typeof epoch !== 'number' || !Number.isFinite(epoch)) return ''
  return fmtDateTimeNumeric(epoch)
}

export default function CodexSessionImportModal({ open, onClose, onImported }: Props) {
  const [filter, setFilter] = useState('')
  const query = useQuery({
    queryKey: ['codex-threads'],
    queryFn: () => api.codexThreads(),
    enabled: open,
    staleTime: 15_000,
  })
  const mutation = useMutation({
    mutationFn: ({ id, mode }: { id: string; mode: 'fork' | 'resume' }) =>
      api.importCodexThread(id, mode),
    onSuccess: slot => {
      onImported(slot)
      onClose()
    },
  })
  const rows = useMemo(() => {
    const needle = filter.trim().toLocaleLowerCase()
    const source = query.data?.threads ?? []
    if (!needle) return source
    return source.filter(thread =>
      `${thread.title}\n${thread.preview}\n${thread.cwd}`.toLocaleLowerCase().includes(needle),
    )
  }, [filter, query.data])
  const pendingId = mutation.isPending ? mutation.variables?.id : ''

  return (
    <Modal
      open={open}
      onClose={onClose}
      title={i18nT('components.codexSessionImport.title')}
      maxWidth={760}
      height="72vh"
      guardAccidentalDismiss={mutation.isPending}
    >
      <div className="flex h-full min-h-0 flex-col gap-3">
        <p className="m-0 text-sm leading-relaxed text-muted">
          {i18nT('components.codexSessionImport.description')}
        </p>
        <div className="flex items-center gap-2">
          <label
            htmlFor="codex-session-import-search"
            className="flex min-w-0 flex-1 items-center gap-2 rounded-md border border-border bg-bg px-2.5 py-2"
          >
            <Search size={14} className="shrink-0 text-muted" aria-hidden="true" />
            <span className="sr-only">{i18nT('components.codexSessionImport.search')}</span>
            <input
              id="codex-session-import-search"
              aria-label={i18nT('components.codexSessionImport.search')}
              value={filter}
              onChange={event => setFilter(event.target.value)}
              placeholder={i18nT('components.codexSessionImport.search')}
              className="min-w-0 flex-1 border-none bg-transparent text-sm text-text outline-none"
            />
          </label>
          <button
            type="button"
            className="flex items-center gap-1.5 rounded-md border border-border bg-bg-elevated px-3 py-2 text-sm text-text hover:bg-bg-hover disabled:opacity-50"
            onClick={() => query.refetch()}
            disabled={query.isFetching}
          >
            {query.isFetching ? <Loader2 size={14} className="animate-spin" /> : <RefreshCw size={14} />}
            {i18nT('components.codexSessionImport.refresh')}
          </button>
        </div>

        {mutation.isError && (
          <ErrorNotice message={mutation.error instanceof Error ? mutation.error.message : i18nT('components.codexSessionImport.importFailed')} />
        )}

        <div className="min-h-0 flex-1 overflow-y-auto rounded-lg border border-border">
          {query.isLoading ? (
            <div className="flex h-full items-center justify-center gap-2 text-sm text-muted">
              <Loader2 size={16} className="animate-spin" />
              {i18nT('components.codexSessionImport.loading')}
            </div>
          ) : query.isError ? (
            <ErrorNotice askAgent message={i18nT('components.codexSessionImport.loadFailed')} className="m-4" />
          ) : rows.length === 0 ? (
            <div className="p-6 text-center text-sm text-muted">{i18nT('components.codexSessionImport.empty')}</div>
          ) : (
            <div className="divide-y divide-border">
              {rows.map(thread => {
                const pending = pendingId === thread.id
                return (
                  <article key={thread.id} className="flex flex-col gap-2.5 p-3.5 hover:bg-bg-hover/40">
                    <div className="flex min-w-0 items-start justify-between gap-3">
                      <div className="min-w-0">
                        <div className="flex flex-wrap items-center gap-2">
                          <h3 className="m-0 truncate text-sm font-semibold text-text-strong">{thread.title}</h3>
                          {thread.imported && (
                            <span className="rounded bg-accent/15 px-1.5 py-0.5 text-[10px] font-medium text-accent">
                              {i18nT('components.codexSessionImport.alreadyContinued')}
                            </span>
                          )}
                        </div>
                        {thread.preview && thread.preview !== thread.title && (
                          <p className="mt-1 line-clamp-2 text-xs leading-relaxed text-muted">{thread.preview}</p>
                        )}
                      </div>
                      <time className="shrink-0 text-[11px] text-muted">{threadDate(thread)}</time>
                    </div>
                    {thread.cwd && <div className="truncate font-mono text-[11px] text-muted" title={thread.cwd}>{thread.cwd}</div>}
                    <div className="flex flex-wrap justify-end gap-2">
                      <button
                        type="button"
                        className="flex items-center gap-1.5 rounded-md bg-accent px-2.5 py-1.5 text-xs font-medium text-accent-fg hover:bg-accent-hover disabled:opacity-50"
                        disabled={mutation.isPending}
                        onClick={() => mutation.mutate({ id: thread.id, mode: 'fork' })}
                      >
                        {pending ? <Loader2 size={13} className="animate-spin" /> : <Copy size={13} />}
                        {i18nT('components.codexSessionImport.importCopy')}
                      </button>
                      <button
                        type="button"
                        className="flex items-center gap-1.5 rounded-md border border-border bg-bg-elevated px-2.5 py-1.5 text-xs text-text hover:bg-bg-hover disabled:cursor-not-allowed disabled:opacity-50"
                        disabled={mutation.isPending || thread.imported}
                        title={thread.imported ? i18nT('components.codexSessionImport.alreadyContinuedHint') : undefined}
                        onClick={() => mutation.mutate({ id: thread.id, mode: 'resume' })}
                      >
                        <Play size={13} />
                        {i18nT('components.codexSessionImport.continueOriginal')}
                      </button>
                    </div>
                  </article>
                )
              })}
            </div>
          )}
        </div>
        <p className="m-0 text-xs leading-relaxed text-muted">
          {i18nT('components.codexSessionImport.resumeWarning')}
        </p>
      </div>
    </Modal>
  )
}
