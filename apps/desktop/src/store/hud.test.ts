import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { $activeGatewayProfile } from '@/store/profile'
import { $sessions } from '@/store/session'
import type { SessionInfo } from '@/types/hermes'

import { $hudActive, $hudProfile, $hudSession, openHud, openHudForProfile, watchHudState } from './hud'

const desktopWindow = window as unknown as { hermesDesktop?: Window['hermesDesktop'] }
const initialHermesDesktop = desktopWindow.hermesDesktop

const open = vi.fn().mockResolvedValue({ ok: true })
const getState = vi.fn().mockResolvedValue({ open: false, profile: null, sessionId: null })
type HudChanged = { handoff?: boolean; open: boolean; profile: null | string; sessionId: null | string }
let changed: ((state: HudChanged) => void) | null = null

function installBridge() {
  desktopWindow.hermesDesktop = {
    hud: {
      open,
      getState,
      onChanged: (callback: (state: HudChanged) => void) => {
        changed = callback

        return () => {
          changed = null
        }
      }
    }
  } as unknown as Window['hermesDesktop']
}

function session(overrides: Partial<SessionInfo>): SessionInfo {
  return { id: 's', title: '', created_at: '', updated_at: '', ...overrides } as SessionInfo
}

beforeEach(() => {
  open.mockClear()
  getState.mockReset()
  getState.mockResolvedValue({ open: false, profile: null, sessionId: null })
  changed = null
  installBridge()
  $hudActive.set(false)
  $hudProfile.set(null)
  $hudSession.set(null)
  $sessions.set([])
  $activeGatewayProfile.set('default')
})

afterEach(() => {
  if (initialHermesDesktop) {
    desktopWindow.hermesDesktop = initialHermesDesktop
  } else {
    delete desktopWindow.hermesDesktop
  }
})

describe('openHud profile targeting (#82285)', () => {
  it('opens a fresh HUD against an explicit profile without foregrounding it', () => {
    $activeGatewayProfile.set('default')

    openHudForProfile('care-acme-dental')

    expect(open).toHaveBeenCalledWith({ sessionId: null, profile: 'care-acme-dental' })
    expect($activeGatewayProfile.get()).toBe('default')
  })

  it('preserves the live conversation when reopening the same profile without a session id', () => {
    $hudActive.set(true)
    $hudProfile.set('care-acme-dental')
    $hudSession.set('technician-session')

    openHudForProfile('care-acme-dental')

    expect(open).toHaveBeenCalledWith({ sessionId: null, profile: 'care-acme-dental' })
    expect($hudSession.get()).toBe('technician-session')
  })

  it('adopts Electron-authoritative profile state before another renderer focuses HUD', () => {
    const dispose = watchHudState()

    changed?.({ open: true, profile: 'care-acme-dental', sessionId: 'technician-session' })
    openHudForProfile('care-acme-dental')

    expect($hudProfile.get()).toBe('care-acme-dental')
    expect($hudSession.get()).toBe('technician-session')
    dispose()
  })

  it('hydrates a late renderer from Electron-authoritative HUD state', async () => {
    getState.mockResolvedValueOnce({
      open: true,
      profile: 'care-acme-dental',
      sessionId: 'technician-session'
    })

    const dispose = watchHudState()
    await Promise.resolve()

    expect($hudActive.get()).toBe(true)
    expect($hudProfile.get()).toBe('care-acme-dental')
    expect($hudSession.get()).toBe('technician-session')
    dispose()
  })

  it('carries the authoritative profile through close before clearing HUD atoms', () => {
    let profileDuringClose: null | string = null
    const onClosed = vi.fn(() => { profileDuringClose = $hudProfile.get() })
    const dispose = watchHudState(onClosed)

    changed?.({ open: true, profile: 'care-acme-dental', sessionId: 'technician-session' })
    changed?.({ handoff: true, open: false, profile: 'care-acme-dental', sessionId: 'technician-session' })

    expect(onClosed).toHaveBeenCalledWith(expect.objectContaining({
      profile: 'care-acme-dental',
      sessionId: 'technician-session',
    }))
    expect(profileDuringClose).toBe('care-acme-dental')
    expect($hudProfile.get()).toBeNull()
    expect($hudSession.get()).toBeNull()
    dispose()
  })

  it('does not let a non-primary renderer claim the close handoff', () => {
    const onClosed = vi.fn()
    const dispose = watchHudState(onClosed)

    changed?.({ open: true, profile: 'care-acme-dental', sessionId: 'technician-session' })
    changed?.({ handoff: false, open: false, profile: 'care-acme-dental', sessionId: 'technician-session' })

    expect(onClosed).not.toHaveBeenCalled()
    dispose()
  })

  it('carries the session-stamped profile when the target belongs to another profile', () => {
    $sessions.set([session({ id: 'abc', profile: 'work' })])
    $activeGatewayProfile.set('default')

    openHud('abc')

    expect(open).toHaveBeenCalledWith({ sessionId: 'abc', profile: 'work' })
  })

  it('falls back to the active gateway profile for an unstamped session', () => {
    $sessions.set([session({ id: 'abc', profile: '' })])
    $activeGatewayProfile.set('work')

    openHud('abc')

    expect(open).toHaveBeenCalledWith({ sessionId: 'abc', profile: 'work' })
  })

  it('uses the active gateway profile when opening without a session', () => {
    $activeGatewayProfile.set('research')

    openHud()

    expect(open).toHaveBeenCalledWith({ sessionId: null, profile: 'research' })
  })

  it('normalizes to default for single-profile users', () => {
    openHud()

    expect(open).toHaveBeenCalledWith({ sessionId: null, profile: 'default' })
  })

  it('uses the active profile when the target session is not in the cache', () => {
    $activeGatewayProfile.set('work')

    openHud('unknown-session')

    expect(open).toHaveBeenCalledWith({ sessionId: 'unknown-session', profile: 'work' })
  })
})
