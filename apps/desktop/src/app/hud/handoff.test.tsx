import { act, cleanup, render, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { $selectedStoredSessionId } from '@/store/session'

import { useHudHandoff } from './handoff'

const mocks = vi.hoisted(() => ({
  ensureGatewayAgent: vi.fn(),
}))

vi.mock('@/store/profile', async importOriginal => ({
  ...(await importOriginal<Record<string, unknown>>()),
  ensureGatewayAgent: mocks.ensureGatewayAgent,
}))

type HudChanged = { handoff?: boolean; open: boolean; profile: null | string; sessionId: null | string }
let changed: ((state: HudChanged) => void) | null = null
const desktopWindow = window as unknown as { hermesDesktop?: Window['hermesDesktop'] }
const initialHermesDesktop = desktopWindow.hermesDesktop

function Harness({ resumeSession }: { resumeSession: (id: string) => unknown }) {
  useHudHandoff({ navigate: vi.fn(), resumeSession })

  return null
}

describe('HUD close handoff profile authority', () => {
  beforeEach(() => {
    changed = null
    mocks.ensureGatewayAgent.mockReset()
    $selectedStoredSessionId.set('technician-session')
    desktopWindow.hermesDesktop = {
      hud: {
        getState: vi.fn().mockResolvedValue({
          open: true,
          profile: 'care-acme-dental',
          sessionId: 'technician-session',
        }),
        onChanged: (callback: (state: HudChanged) => void) => {
          changed = callback

          return () => { changed = null }
        },
      },
    } as unknown as Window['hermesDesktop']
  })

  afterEach(() => {
    cleanup()
    $selectedStoredSessionId.set(null)

    if (initialHermesDesktop) {
      desktopWindow.hermesDesktop = initialHermesDesktop
    } else {
      delete desktopWindow.hermesDesktop
    }
  })

  it('ensures the authoritative care profile before resuming an uncached HUD session', async () => {
    const order: string[] = []
    mocks.ensureGatewayAgent.mockImplementation(async (connectionId, profile) => {
      order.push(`ensure:${String(connectionId)}:${profile}`)
    })
    const resumeSession = vi.fn(async id => { order.push(`resume:${id}`) })
    render(<Harness resumeSession={resumeSession} />)

    act(() => changed?.({
      handoff: true,
      open: false,
      profile: 'care-acme-dental',
      sessionId: 'technician-session',
    }))

    await waitFor(() => expect(resumeSession).toHaveBeenCalledWith('technician-session'))
    expect(order).toEqual([
      'ensure:null:care-acme-dental',
      'resume:technician-session',
    ])
  })

  it('cancels an obsolete close handoff when HUD reopens during profile ensure', async () => {
    let releaseEnsure!: () => void
    mocks.ensureGatewayAgent.mockImplementation(() => new Promise<void>(resolve => { releaseEnsure = resolve }))
    const resumeSession = vi.fn()
    render(<Harness resumeSession={resumeSession} />)

    act(() => changed?.({
      handoff: true,
      open: false,
      profile: 'care-acme-dental',
      sessionId: 'technician-session',
    }))
    act(() => changed?.({
      open: true,
      profile: 'care-other',
      sessionId: 'newer-session',
    }))
    releaseEnsure()
    await act(async () => { await Promise.resolve() })

    expect(resumeSession).not.toHaveBeenCalled()
  })
})
