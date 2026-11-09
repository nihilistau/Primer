/* api — the Primer's HTTP client. JSON helpers plus openPage, which streams SSE. */
import { parseSSE } from './sse.js'

/* Every request carries X-Primer: 1, a header a cross-site <img> or form can't send (the
 * server requires it where a GET does model work), and every POST a JSON body: the server
 * refuses any other Content-Type (415). */
const PRIMER = { 'X-Primer': '1' }

async function j(method, url, body, signal, extra) {
  if (method === 'POST') body = body || {}
  const r = await fetch(url, {
    method,
    headers: body ? { ...PRIMER, 'Content-Type': 'application/json' } : PRIMER,
    body: body ? JSON.stringify(body) : undefined,
    signal,
    ...extra,
  })
  const data = await r.json().catch(() => ({}))
  if (!r.ok) throw Object.assign(new Error(data.error || data.message || `${method} ${url}: ${r.status}`), { status: r.status })
  return data
}

export const status = () => j('GET', '/v1/status')
export const setTier = (name) => j('POST', '/v1/tier', { name })
export const serverAction = (role, action) => j('POST', `/v1/servers/${encodeURIComponent(role)}/${action}`)
export const setSourceMode = (mode) => j('POST', '/v1/source-mode', { mode })
export const setThinkingAhead = (setting) => j('POST', '/v1/thinking-ahead', { setting })
export const page = (id) => j('GET', `/v1/pages/${encodeURIComponent(id)}`)
export const branches = (node, phrase) =>
  j('GET', `/v1/branches?node=${encodeURIComponent(node)}&phrase=${encodeURIComponent(phrase)}`)
export const graph = () => j('GET', '/v1/graph')

/* POST a JSON body and stream the SSE reply to onEvent({event, data}). Resolves when the
 * stream ends; rejects on a non-2xx reply (a JSON {error}), a network failure, an abort
 * (AbortError) or a throwing onEvent. `signal` is an AbortSignal. */
export async function postStream(url, body, onEvent, signal, what = 'request') {
  const r = await fetch(url, {
    method: 'POST', headers: { ...PRIMER, 'Content-Type': 'application/json' },
    body: JSON.stringify(body), signal,
  })
  if (!r.ok) {
    const data = await r.json().catch(() => ({}))
    throw new Error(data.error || data.message || `${what}: ${r.status}`)
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

/* POST /v1/pages/open and stream its events (node, delta, done, highlights, error) to
 * onEvent({event, data}). Resolves when the stream ends, which may be before `done` if
 * Primer stopped mid-page (see reader/streamEnd.js). */
export const openPage = (body, onEvent, signal) => postStream('/v1/pages/open', body, onEvent, signal, 'open page')

/* the Browser: a page as Markdown, search, and the two model streams (ask, click-through) */
export const browse = (u, signal) => j('GET', `/v1/browse?u=${encodeURIComponent(u)}`, undefined, signal)
export const browseSearch = (q, where, signal) =>
  j('GET', `/v1/browse/search?q=${encodeURIComponent(q)}&where=${encodeURIComponent(where)}`, undefined, signal)
export const browseAsk = (u, question, onEvent, signal) =>
  postStream('/v1/browse/ask', { u, question }, onEvent, signal, 'ask')
export const browseAgent = (u, goal, onEvent, signal) =>
  postStream('/v1/browse/agent', { u, goal }, onEvent, signal, 'click through')

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

/* Setup: kits, the plan, the run, approvals, the model mover and update checks. Every run
 * action answers with the installer's state; updates does network work, so it (like every
 * call) carries X-Primer. */
export const setup = () => j('GET', '/v1/setup')
export const setupPlan = (kit, choices, root) => j('POST', '/v1/setup/plan', { kit, choices: choices || {}, root })
// names the plan the window shows: the server answers 409 if the pending plan is another one
export const setupStart = (kit, root, choices) => j('POST', '/v1/setup/start', { kit, root, choices: choices || {} })
export const setupPause = () => j('POST', '/v1/setup/pause')
export const setupResume = () => j('POST', '/v1/setup/resume')
export const setupCancel = (item) => j('POST', '/v1/setup/cancel', { item })
// `licence` is the licence's key as the plan lists it (name and link)
export const setupLicence = (licence) => j('POST', '/v1/setup/licence', { licence })
export const setupApprove = (path, sha256) => j('POST', '/v1/setup/approve', { path, sha256 })
export const setupApproveRelease = (item) => j('POST', '/v1/setup/approve-release', { item })
export const setupUpdates = (refresh = false) => j('GET', '/v1/setup/updates' + (refresh ? '?refresh=1' : ''))
export const setupModels = () => j('GET', '/v1/setup/models')
export const setupMove = (paths, dest) => j('POST', '/v1/setup/move', { paths, dest })
export const setupMoveCancel = () => j('POST', '/v1/setup/move/cancel')

/* the reader model (Phase 10): its state, notes and switch, reset, a forced summary, and how
 * long a page was on screen (204, no body; keepalive, so a report sent as the tab goes away
 * still arrives) */
export const readerState = () => j('GET', '/v1/reader')
export const saveReader = (patch) => j('POST', '/v1/reader', patch)
export const resetReader = () => j('POST', '/v1/reader/reset')
export const refreshReader = () => j('POST', '/v1/reader/refresh')
export const readerDwell = (nodeId, seconds) => j('POST', '/v1/reader/dwell', { node_id: nodeId, seconds }, undefined, { keepalive: true })

/* Test me and Review. make may wait on the GPU gate (a real request, like a page). */
export const quizMake = (nodeId, fresh = false) => j('POST', '/v1/quiz/make', { node_id: nodeId, fresh })
export const quizFor = (nodeId) => j('GET', `/v1/quiz?node_id=${encodeURIComponent(nodeId)}`)
// revealed: the model answer was shown first (marked not yet without asking the model)
export const quizAnswer = (itemId, answer, revealed = false) =>
  j('POST', '/v1/quiz/answer', revealed ? { item_id: itemId, answer, revealed: true } : { item_id: itemId, answer })
export const quizDue = (limit = 20) => j('GET', `/v1/quiz/due?limit=${limit}`)
export const quizItem = (id) => j('GET', `/v1/quiz/item?id=${encodeURIComponent(id)}`)

/* the Quest: the current one, a build (from recent reading, or a page's trail), a door, a
 * skip, leaving it */
export const quest = () => j('GET', '/v1/quest')
export const questBuild = (fromNodeId) => j('POST', '/v1/quest', fromNodeId ? { from_node_id: fromNodeId } : {})
export const questAnswer = (questId, answer) => j('POST', '/v1/quest/answer', { quest_id: questId, answer })
export const questSkip = (questId) => j('POST', '/v1/quest/skip', { quest_id: questId })
export const questAbandon = (questId) => j('POST', '/v1/quest/abandon', { quest_id: questId })

/* the Tale (Phase 11): {current, tales, preview}; a new tale (invented with no seed); reopen an
 * older one; the next chapter (the scenes are written in the request: 202 {story_id, n}); edit
 * the bible ({name?, description?, look?, remove_thread?, remove_place?}); retry a failed bible
 * update */
export const tales = () => j('GET', '/v1/tales')
export const taleStart = (seed) => j('POST', '/v1/tales', seed ? { seed } : {})
export const taleOpen = (taleId) => j('POST', '/v1/tales/open', { tale_id: taleId })
export const taleNext = (pages) => j('POST', '/v1/tales/next', pages ? { pages } : {})
export const taleEdit = (taleId, fields) => j('POST', '/v1/tales/edit', { ...fields, tale_id: taleId })
export const taleRetry = (taleId, n) => j('POST', '/v1/tales/retry-update', { tale_id: taleId, n })
