import { render, screen } from '@testing-library/react'
import { beforeAll, describe, expect, it, vi } from 'vitest'
import { registerThemeBranding } from '../themeBranding'
import { ShellAside } from '../components/OnboardingChapterShell'

let activeTheme = 'unregistered-onboarding-theme'

vi.mock('../hooks/useTheme', () => ({
  useTheme: () => ({ colorTheme: activeTheme }),
}))

const copy = {
  ariaLabel: 'Setup',
  panelHeadline: 'Bring your crew with you.',
  panelBody: 'Import supported setup.',
  panelFootnote: 'Credentials stay where they are.',
}

function EditionMark({ className }: { className?: string }) {
  return <img data-testid="edition-onboarding-mark" className={className} alt="" />
}

function EditionDecorations() {
  return <div data-testid="edition-onboarding-decorations" />
}

describe('onboarding theme branding seam', () => {
  beforeAll(() => {
    registerThemeBranding({
      'edition-onboarding-theme': {
        onboarding: {
          mark: EditionMark,
          decorations: EditionDecorations,
        },
      },
    })
  })

  it('retains the stock mascot treatment when no onboarding branding is registered', () => {
    activeTheme = 'unregistered-onboarding-theme'
    const { container } = render(<ShellAside copy={copy} />)

    expect(screen.queryByTestId('edition-onboarding-mark')).toBeNull()
    expect(screen.queryByTestId('edition-onboarding-decorations')).toBeNull()
    expect(container.querySelectorAll('svg')).toHaveLength(5)
  })

  it('uses edition mark and decorations without rendering stock mascots', () => {
    activeTheme = 'edition-onboarding-theme'
    const { container } = render(<ShellAside copy={copy} />)

    expect(screen.getByTestId('edition-onboarding-mark')).toHaveClass('h-8', 'w-8')
    expect(screen.getByTestId('edition-onboarding-decorations')).toBeInTheDocument()
    expect(container.querySelectorAll('svg')).toHaveLength(0)
  })
})
