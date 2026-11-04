/* openBrowser — open an address in the Browser. A leaf module (like openImage.js) so the Reader
 * can use it without an import cycle. A plain open reuses the most recent Browser window (it
 * listens for OPEN_BROWSER_EVENT and marks the detail handled); with none, or newWindow, a new
 * window opens. */
import * as wm from './windowManager.js'

export const OPEN_BROWSER_EVENT = 'primer:open-browser'

export function openBrowser(u, { newWindow = false } = {}) {
  if (!newWindow && typeof window !== 'undefined') {
    const latest = wm.getWindows().filter(w => w.appId === 'browser').sort((a, b) => b.z - a.z)[0]
    if (latest) {
      const detail = { u, windowId: latest.id, handled: false }
      window.dispatchEvent(new CustomEvent(OPEN_BROWSER_EVENT, { detail }))
      if (detail.handled) { wm.focus(latest.id); return latest.id }
    }
  }
  return wm.open('browser', { props: { start: u }, w: 900, h: 680 })
}
