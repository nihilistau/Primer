/* api — the Primer's HTTP client. JSON helpers plus openPage, which streams SSE. */
import { parseSSE } from './sse.js'

/* Every request carries X-Primer: 1, a header a cross-site <img> or form can't send (the
 * server requires it where a GET does model work), and every POST a JSON body: the server
 * refuses any other Content-Type (415). */
const PRIMER = { 'X-Primer': '1' }

async function j(method, url, body) {
  if (method === 'POST') body = body || {}
  const r = await fetch(url, {
    method,
    headers: body ? { ...PRIMER, 'Content-Type': 'application/json' } : PRIMER,
    body: body ? JSON.stringify(body) : undefined,
  })
  const data = await r.json().catch(() => ({}))
  if (!r.ok) throw Object.assign(new Error(data.error || data.message || `${method} ${url}: ${r.status}`), { status: r.status })
  return data
}

export const status = () => j('GET', '/v1/status')
export const setTier = (name) => j('POST', '/v1/tier', { name })
export const serverAction = (role, action) => j('POST', `/v1/servers/${encodeURIComponent(role)}/${action}`)
export const setSourceMode = (mode) => j('POST', '/v1/source-mode', { mode })
export const page = (id) => j('GET', `/v1/pages/${encodeURIComponent(id)}`)
export const branches = (node, phrase) =>
  j('GET', `/v1/branches?node=${encodeURIComponent(node)}&phrase=${encodeURIComponent(phrase)}`)
export const graph = () => j('GET', '/v1/graph')

/* POST /v1/pages/open and stream its events (node, delta, done, highlights, error) to
 * onEvent({event, data}). Resolves when the stream ends, which may be before `done` if
 * Primer stopped mid-page (see reader/streamEnd.js); rejects on a non-2xx reply (400
 * carries a JSON error), a network failure, an abort (AbortError) or a throwing onEvent.
 * `signal` is an AbortSignal. */
export async function openPage(body, onEvent, signal) {
  const r = await fetch('/v1/pages/open', {
    method: 'POST', headers: { ...PRIMER, 'Content-Type': 'application/json' },
    body: JSON.stringify(body), signal,
  })
  if (!r.ok) {
    const data = await r.json().catch(() => ({}))
    throw new Error(data.error || data.message || `open page: ${r.status}`)
  }
  const reader = r.body.getReader()
  const dec = new TextDecoder()
  let carry = ''
  try {
    for (;;) {
      const { done, value } = await reader.read()
      const out = parseSSE(done ? dec.decode() : dec.decode(value, { stream: true }), carry)
      carry = out.carry
      out.events.forEach(onEvent)
      if (done) break
    }
  } finally {
    reader.cancel().catch(() => {})   // a throwing onEvent must not leave the body open
  }
}

/* images */
export const intent = (text) => j('POST', '/v1/intent', { text })
export const createImage = (body) => j('POST', '/v1/images', body)
export const imageJob = (id) => j('GET', `/v1/images/${encodeURIComponent(id)}`)
export const cancelImage = (id) => j('POST', `/v1/images/${encodeURIComponent(id)}/cancel`)
export const imageModels = () => j('GET', '/v1/image-models')
export const saveImageModel = (model) => j('POST', '/v1/image-models', { model })
export const deleteImageModel = (id) => j('POST', '/v1/image-models/delete', { id })
export const setDefaultImageModel = (id) => j('POST', '/v1/image-models/default', { id })
export const scanFolder = (folder) => j('POST', '/v1/image-models/scan', { folder })
/* only a <sha256>.png name is a media file; anything else is no URL at all */
export const mediaUrl = (file) => (typeof file === 'string' && /^[0-9a-f]{64}\.png$/.test(file) ? `/v1/media/${file}` : '')
/* likewise a <sha256>.webm name for a video */
export const videoUrl = (file) => (typeof file === 'string' && /^[0-9a-f]{64}\.webm$/.test(file) ? `/v1/media/${file}` : '')

/* maps */
export const createMap = (body) => j('POST', '/v1/maps', body)
export const mapJob = (id) => j('GET', `/v1/maps/${encodeURIComponent(id)}`)
export const mapAgain = (id) => j('POST', `/v1/maps/${encodeURIComponent(id)}/again`)
export const mapTileJson = () => j('GET', '/v1/map/tilejson')

/* stories (Listen is a story of kind "listen") */
export const createStory = (body) => j('POST', '/v1/stories', body)
export const storyJob = (id) => j('GET', `/v1/stories/${encodeURIComponent(id)}`)
export const storyAgain = (id) => j('POST', `/v1/stories/${encodeURIComponent(id)}/again`)
/* only a <sha256>.wav name is a narration file */
export const audioUrl = (file) => (typeof file === 'string' && /^[0-9a-f]{64}\.wav$/.test(file) ? `/v1/media/${file}` : '')
