import { afterEach, beforeEach, expect, it, vi } from 'vitest'

const desktopWindow = window as unknown as { hermesDesktop?: Window['hermesDesktop'] }
const initialHermesDesktop = desktopWindow.hermesDesktop

beforeEach(() => {
  vi.resetModules()
  window.localStorage.clear()
  window.history.replaceState({}, '', '/?win=hud&profile=care-acme')
})

afterEach(() => {
  window.history.replaceState({}, '', '/')
  window.localStorage.clear()

  if (initialHermesDesktop) {
    desktopWindow.hermesDesktop = initialHermesDesktop
  } else {
    delete desktopWindow.hermesDesktop
  }
})

it('HUD module boot paints locally without publishing shared theme authority', async () => {
  const setNativeTheme = vi.fn()
  const setTitleBarTheme = vi.fn()

  desktopWindow.hermesDesktop = {
    setNativeTheme,
    setTitleBarTheme,
  } as unknown as Window['hermesDesktop']
  window.localStorage.setItem('hermes-desktop-active-profile-v1', 'default')
  window.localStorage.setItem('hermes-boot-background', '#operator')
  window.localStorage.setItem('hermes-boot-color-scheme', 'light')

  await import('./context')

  expect(window.localStorage.getItem('hermes-desktop-active-profile-v1')).toBe('default')
  expect(window.localStorage.getItem('hermes-boot-background')).toBe('#operator')
  expect(window.localStorage.getItem('hermes-boot-color-scheme')).toBe('light')
  expect(setNativeTheme).not.toHaveBeenCalled()
  expect(setTitleBarTheme).not.toHaveBeenCalled()
})
