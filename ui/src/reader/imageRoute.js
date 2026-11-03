/* imageRoute — what a typed line means (an image or a page) and whether the Image buttons work. */
import * as api from '../api.js'

/* Ask the server whether `text` is a story, map, video or image request (checked in that order, as the
 * server does). A map request routes to its place as a typed page (the Map window follows once
 * the page is written); a video or image routes to a createImage body (the page's node, or the subject as a concept
 * when the reader is empty; a video adds kind video); anything else, or an intent call that
 * fails, is a page request as before. */
export async function routePrompt(text, { nodeId } = {}) {
  let r = {}
  try { r = (await api.intent(text)) || {} } catch { /* just open a page */ }
  const where = (subject) => (nodeId ? { node_id: nodeId, subject } : { concept: subject, subject })
  // a map request opens the place's page as a typed page; the Map window follows once its node is known
  // a story request does the same: the topic's page first, then its story once the node is known
  if (r.story) return { kind: 'story', body: { concept: r.story, via: 'typed', ...(nodeId ? { from_id: nodeId } : {}) } }
  if (r.map) return { kind: 'map', body: { concept: r.map, via: 'typed', ...(nodeId ? { from_id: nodeId } : {}) } }
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

/* {enabled, reason} for the Map button from a /v1/status reply */
export function mapButton(status) {
  const a = status && status.map_available
  if (a && a.ok) return { enabled: true, reason: '' }
  return { enabled: false, reason: (a && a.reason) || 'Maps are not available right now.' }
}
export const mapChecking = { enabled: false, checking: true, reason: 'Checking whether maps are available…' }
export const mapCheckFailed = (e) =>
  ({ enabled: false, reason: `Couldn't check map availability: ${(e && e.message) || 'Primer did not answer.'}` })

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

/* The map job for a typed map request, made once the place's page is written: a phrase map of
 * the typed subject, so "show Paris on a map" always pins Paris (a whole-page map would pin
 * whatever the page names, maybe not Paris at all). */
export const typedMapBody = (nodeId, subject) => ({ node_id: nodeId, phrase: subject, subject })

/* {enabled, reason} for the Listen and Story buttons from a /v1/status reply */
export function storyButton(status) {
  const a = status && status.voice_available
  if (a && a.ok) return { enabled: true, reason: '' }
  return { enabled: false, reason: (a && a.reason) || 'Narration is not available right now.' }
}
export const storyChecking = { enabled: false, checking: true, reason: 'Checking whether narration is available…' }
export const storyCheckFailed = (e) =>
  ({ enabled: false, reason: `Couldn't check narration availability: ${(e && e.message) || 'Primer did not answer.'}` })
