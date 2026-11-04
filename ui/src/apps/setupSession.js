/* setupSession — has Setup been opened in this browser session? The first-run check opens
 * it once; a user who closed it isn't shown it again until a new session. A leaf module, so
 * the Desktop and the window can share it without an import cycle. sessionStorage may be
 * unavailable (privacy modes); the module flag still holds for this page. */
const KEY = 'primer.setup.opened'
let opened = false

export function setupOpened() {
  if (opened) return true
  try { return sessionStorage.getItem(KEY) === '1' } catch { return false }
}

export function markSetupOpened() {
  opened = true
  try { sessionStorage.setItem(KEY, '1') } catch { /* the module flag is enough */ }
}
