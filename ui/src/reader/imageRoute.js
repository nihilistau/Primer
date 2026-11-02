/* imageRoute — what a typed line means (an image or a page) and whether the Image buttons work. */
import * as api from '../api.js'

/* Ask the server whether `text` is an image request. An image routes to a createImage body
 * (the page's node, or the subject as a concept when the reader is empty); anything else, or
 * an intent call that fails, is a page request as before. */
export async function routePrompt(text, { nodeId } = {}) {
  let subject = null
  try { subject = (await api.intent(text)).image || null } catch { /* just open a page */ }
  if (subject) return { kind: 'image', body: nodeId ? { node_id: nodeId, subject } : { concept: subject, subject } }
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
