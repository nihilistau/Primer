/* openImage — open an Image window. A leaf module (no app imports) so the Reader can use it
 * without an import cycle through apps.jsx; sizes match the registry's image entry. */
import * as wm from './windowManager.js'

export const openImageWindow = (jobId, subject) =>
  wm.open('image', { props: { jobId, subject }, w: 640, h: 720 })
