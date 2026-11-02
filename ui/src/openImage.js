/* openImage — open an Image, Video or Map window. A leaf module (no app imports) so the Reader can use
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

export const openMapWindow = (mapId, subject, nodeId) => {
  const id = wm.open('map', { props: { mapId, subject, nodeId }, w: 760, h: 640 })
  announce()
  return id
}

/* A place on a map opens its page. The reader showing the mapped page takes it (it listens for
 * OPEN_PLACE_EVENT and marks the detail handled); with none, or on shift/middle click, a new
 * reader opens. `body` is an open-page body: {concept, via: 'general', from_id, phrase}. */
export const OPEN_PLACE_EVENT = 'primer:open-place'

export const openPlace = (body, { newWindow = false } = {}) => {
  if (!newWindow && typeof window !== 'undefined') {
    const detail = { body, handled: false }
    window.dispatchEvent(new CustomEvent(OPEN_PLACE_EVENT, { detail }))
    if (detail.handled) return
  }
  wm.open('reader', { props: { start: body }, w: 720, h: 640 })
}
