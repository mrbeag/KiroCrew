import { AlertCircle, Clock3, Coins, Gauge, Loader2 } from 'lucide-react'

import type { CodexRateLimitWindow, CodexUsagePayload } from '../api/client'
import { fmtDateTime, fmtNumber, fmtPercent, fmtRelative } from '../i18n/format'
import { i18nT } from '../i18n/t'
import Modal from './Modal'

export type CodexUsageState = CodexUsagePayload | null | 'failed'

interface CodexUsageModalProps {
  open: boolean
  onClose: () => void
  usage: CodexUsageState
}

function titleCase(value: string): string {
  return value
    .replace(/[_-]+/g, ' ')
    .replace(/\b\w/g, letter => letter.toUpperCase())
}

function windowLabel(window: CodexRateLimitWindow, fallback: string): string {
  const minutes = window.window_minutes
  if (minutes == null || minutes <= 0) return fallback
  if (minutes % 10_080 === 0) {
    const weeks = minutes / 10_080
    return weeks === 1
      ? i18nT('components.codexUsageModal.weekly_window')
      : i18nT('components.codexUsageModal.week_window', { count: fmtNumber(weeks) })
  }
  if (minutes % 1_440 === 0) {
    const days = minutes / 1_440
    return days === 1
      ? i18nT('components.codexUsageModal.daily_window')
      : i18nT('components.codexUsageModal.day_window', { count: fmtNumber(days) })
  }
  if (minutes % 60 === 0) {
    const hours = minutes / 60
    return i18nT('components.codexUsageModal.hour_window', { count: fmtNumber(hours) })
  }
  return i18nT('components.codexUsageModal.minute_window', { count: fmtNumber(minutes) })
}

function RateWindow({ window, fallback }: { window: CodexRateLimitWindow; fallback: string }) {
  const label = windowLabel(window, fallback)
  const used = Math.min(Math.max(window.used_percent, 0), 100)
  const remaining = 100 - used
  const reset = window.resets_at && window.resets_at > 0 ? window.resets_at : null

  return (
    <section className="rounded-lg border border-border px-3 py-3" aria-label={label}>
      <div className="flex items-center justify-between gap-4">
        <span className="flex items-center gap-2 text-[13px] font-medium text-text">
          <Gauge className="lucide-inline text-accent" /> {label}
        </span>
        <span className="font-mono text-[13px] font-semibold tabular-nums text-text">
          {i18nT('components.codexUsageModal.remaining', { percent: fmtPercent(remaining / 100) })}
        </span>
      </div>
      <div
        role="progressbar"
        aria-label={`${label} remaining`}
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuenow={remaining}
        className="mt-3 h-2 w-full overflow-hidden rounded-full bg-border"
      >
        <div
          className="h-full rounded-full bg-accent transition-all"
          style={{ width: `${remaining}%` }}
        />
      </div>
      <div className="mt-2 flex items-start justify-end gap-4 text-[12px] text-muted">
        {reset && (
          <span className="text-right">
            {i18nT('components.codexUsageModal.resets', { when: fmtRelative(reset) })}
            <span className="block text-[11px] opacity-75">{fmtDateTime(reset)}</span>
          </span>
        )}
      </div>
    </section>
  )
}

function UsageBody({ usage }: { usage: CodexUsageState }) {
  if (usage === null) {
    return (
      <div className="flex items-center gap-2 rounded-lg border border-border bg-bg-elevated/40 p-4 text-[13px] text-muted">
        <Loader2 className="lucide-inline animate-spin" /> {i18nT('components.codexUsageModal.checking')}
      </div>
    )
  }
  if (usage === 'failed' || !usage.available) {
    return (
      <div className="flex items-center gap-2 rounded-lg border border-border bg-bg-elevated/40 p-4 text-[13px] text-muted">
        <AlertCircle className="lucide-inline" /> {i18nT('components.codexUsageModal.unavailable')}
      </div>
    )
  }

  const credits = usage.credits
  const creditValue = credits?.unlimited
    ? i18nT('components.codexUsageModal.unlimited')
    : credits?.balance != null
      ? credits.balance
      : credits?.hasCredits === false
        ? i18nT('components.codexUsageModal.no_additional_credits')
        : null

  return (
    <div className="flex flex-col gap-3">
      {(usage.plan || usage.limit_name || creditValue) && (
        <div className="rounded-lg border border-border px-3">
          {usage.plan && (
            <div className="flex items-baseline justify-between gap-4 border-b border-border py-2 last:border-b-0">
              <span className="text-[12px] text-muted">{i18nT('components.codexUsageModal.plan')}</span>
              <span className="text-[13px] font-medium text-text">{titleCase(usage.plan)}</span>
            </div>
          )}
          {usage.limit_name && (
            <div className="flex items-baseline justify-between gap-4 border-b border-border py-2 last:border-b-0">
              <span className="text-[12px] text-muted">{i18nT('components.codexUsageModal.limit')}</span>
              <span className="text-[13px] font-medium text-text">{titleCase(usage.limit_name)}</span>
            </div>
          )}
          {creditValue && (
            <div className="flex items-baseline justify-between gap-4 border-b border-border py-2 last:border-b-0">
              <span className="text-[12px] text-muted">{i18nT('components.codexUsageModal.credits')}</span>
              <span className="text-[13px] font-medium text-text">{creditValue}</span>
            </div>
          )}
        </div>
      )}
      {usage.primary && <RateWindow window={usage.primary} fallback={i18nT('components.codexUsageModal.primary_window')} />}
      {usage.secondary && <RateWindow window={usage.secondary} fallback={i18nT('components.codexUsageModal.secondary_window')} />}
      {!usage.primary && !usage.secondary && (
        <div className="flex items-center gap-2 rounded-lg border border-border bg-bg-elevated/40 p-4 text-[13px] text-muted">
          <Clock3 className="lucide-inline" /> {i18nT('components.codexUsageModal.no_windows')}
        </div>
      )}
      <p className="text-[11px] leading-relaxed text-muted">
        {i18nT('components.codexUsageModal.scope')}
      </p>
    </div>
  )
}

export default function CodexUsageModal({ open, onClose, usage }: CodexUsageModalProps) {
  return (
    <Modal
      open={open}
      onClose={onClose}
      title={<span className="flex items-center gap-2"><Coins className="lucide-inline" /> {i18nT('components.codexUsageModal.codex_usage')}</span>}
      maxWidth={460}
    >
      <UsageBody usage={usage} />
    </Modal>
  )
}
