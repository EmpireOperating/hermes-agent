import { useStore } from '@nanostores/react'

import { NEW_SESSION_TITLE } from '@/lib/chat-runtime'
import { useStoreSelector } from '@/lib/use-session-slice'
import { $draftTitles, draftTitleIn, newSessionDraftScope } from '@/store/composer'
import { $activeGatewayProfile } from '@/store/profile'

export interface SessionDraftTitleProps {
  /** The draft's composer key — a tile's stored session id, or null for the
   *  new chat that has no session yet. */
  scope: null | string
}

/**
 * A DRAFT'S NAME — what an unsent session is called until it has a real one.
 *
 * The tab of a session that has never been sent renders this instead of its
 * registered title, because the name moves with the composer: every debounced
 * stash republishes it. Re-registering the contribution at that rate would
 * re-render the whole panes area, so the label subscribes for itself and its
 * own key only.
 *
 * Falls back to the placeholder rather than going blank, so an emptied composer
 * reads the same as one never typed into.
 */
export function SessionDraftTitle({ scope }: SessionDraftTitleProps) {
  const activeProfile = useStore($activeGatewayProfile)
  const resolvedScope = scope ?? newSessionDraftScope(activeProfile)

  return useStoreSelector($draftTitles, titles => draftTitleIn(titles, resolvedScope)) || NEW_SESSION_TITLE
}
