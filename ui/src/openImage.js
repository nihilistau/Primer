/* openImage — open an Image or Video window. A leaf module (no app imports) so the Reader can use
 * it without an import cycle through apps.jsx; sizes match the registry's image and video entries.
 * Each open also announces MEDIA_EVENT on window: a new job exists, so a Reader showing its page
 * refreshes its thumbnail strip (jobs started from an Image or Video window included). */
import * as wm from './windowManager.js'

export const MEDIA_EVENT = 'primer:media'

const announce = () => { if (typeof window !== 'undefined') window.dispatchEvent(new Event(MEDIA_EVENT)) }

export const openImageWindow = (jobId, subject) => {
  const id = wm.open('image', { props: { jobId, subject }, w: 640, h: 720 })
  announce()
  return id
}

export const openVideoWindow = (jobId, subject) => {
  const id = wm.open('video', { props: { jobId, subject }, w: 640, h: 760 })
  announce()
  return id
}
