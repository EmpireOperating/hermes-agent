export interface HudStatePayload {
  handoff: boolean
  open: boolean
  profile: null | string
  sessionId: null | string
}

/** Every renderer learns HUD visibility, but only Electron's primary window may
 * claim the close handoff and rebind the session's single gateway transport. */
export function hudStatePayload(
  open: boolean,
  profile: null | string,
  sessionId: null | string,
  isMainWindow: boolean,
): HudStatePayload {
  return {
    handoff: !open && isMainWindow,
    open,
    profile,
    sessionId,
  }
}
