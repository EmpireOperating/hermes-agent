import { describe, expect, it } from 'vitest'

import { hudStatePayload } from './hud-state-routing'

describe('HUD state routing', () => {
  it('grants close handoff only to Electron primary window', () => {
    expect(hudStatePayload(false, 'care-acme', 'session-1', true)).toEqual({
      handoff: true,
      open: false,
      profile: 'care-acme',
      sessionId: 'session-1',
    })
    expect(hudStatePayload(false, 'care-acme', 'session-1', false)).toEqual({
      handoff: false,
      open: false,
      profile: 'care-acme',
      sessionId: 'session-1',
    })
  })

  it('never grants handoff while HUD is open', () => {
    expect(hudStatePayload(true, 'care-acme', 'session-1', true).handoff).toBe(false)
  })
})
