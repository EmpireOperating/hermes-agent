import { act, cleanup, render } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'

import { clearSessionDraft, stashSessionDraft } from '@/store/composer'
import { $activeGatewayProfile } from '@/store/profile'

import { SessionDraftTitle } from './session-draft-title'

describe('SessionDraftTitle profile isolation', () => {
  afterEach(() => {
    cleanup()
    $activeGatewayProfile.set('default')
    clearSessionDraft(null)
    $activeGatewayProfile.set('client-b')
    clearSessionDraft(null)
    $activeGatewayProfile.set('default')
  })

  it('switches settled fresh-draft titles when only the active profile changes', () => {
    $activeGatewayProfile.set('default')
    stashSessionDraft(null, 'Default confidential draft', [])
    $activeGatewayProfile.set('client-b')
    stashSessionDraft(null, 'Client B draft', [])
    $activeGatewayProfile.set('default')

    const view = render(<SessionDraftTitle scope={null} />)
    expect(view.getByText('Default confidential draft')).toBeTruthy()

    act(() => $activeGatewayProfile.set('client-b'))
    expect(view.getByText('Client B draft')).toBeTruthy()
    expect(view.queryByText('Default confidential draft')).toBeNull()
  })
})
