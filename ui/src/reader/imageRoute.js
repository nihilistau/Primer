/* imageRoute — what a typed line means (an image or a page) and whether the Image buttons work. */
import * as api from '../api.js'

/* Ask the server whether `text` is a video or an image request (video first, as the server
 * checks it). Either routes to a createImage body (the page's node, or the subject as a concept
 * when the reader is empty; a video adds kind video); anything else, or an intent call that
 * fails, is a page request as before. */
export async function routePrompt(text, { nodeId } = {}) {
  let r = {}
  try { r = (await api.intent(text)) || {} } catch { /* just open a page */ }
  const where = (subject) => (nodeId ? { node_id: nodeId, subject } : { concept: subject, subject })
  if (r.video) return { kind: 'video', body: { ...where(r.video), kind: 'video' } }
  if (r.image) return { kind: 'image', body: where(r.image) }
  return { kind: 'page', body: { concept: text, via: 'typed', ...(nodeId ? { from_id: nodeId } : {}) } }
}

/* {enabled, reason} for an Image button from a /v1/status reply */
export function imageButton(status) {
  const a = status && status.image_available
  if (a && a.ok) return { enabled: true, reason: '' }
  return { enabled: false, reason: (a && a.reason) || 'Images are not available right now.' }
}

/* the button state while status() is in flight / after it failed */
export const imageChecking = { enabled: false, checking: true, reason: 'Checking whether images are available…' }
export const imageCheckFailed = (e) =>
  ({ enabled: false, reason: `Couldn't check image availability: ${(e && e.message) || 'Primer did not answer.'}` })

/* 'busy' (not finished), 'done', or 'failed' (failed or cancelled) */
export function thumbState(m) {
  if (m.status === 'done') return 'done'
  if (m.status === 'failed' || m.status === 'cancelled') return 'failed'
  return 'busy'
}
