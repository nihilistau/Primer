/* storyJob — what a story means to the Story window: its status line, when polling can stop and
 * how a scene's spoken text is cut for highlighting. Pure apart from pollStory. */
import { storyJob } from '../api.js'
import { splitByPhrases } from '../reader/highlight.js'
import { createPoller } from './poller.js'

const ENDED = new Set(['done', 'failed'])
const PIC_ENDED = new Set(['done', 'failed', 'cancelled'])

export const isEnded = (s) => !!s && ENDED.has(s.status)

/* Ended, with every scene picture finished too: pictures go on after the story is done (they
 * queue on the GPU), so polling must not stop at `done`. */
export const isSettled = (s) =>
  isEnded(s) && (s.scenes || []).every(sc => !sc.picture || PIC_ENDED.has(sc.picture.status))

export function storyStatusLabel(s) {
  if (!s) return 'Starting…'
  const scenes = s.scenes || []
  const listen = s.kind === 'listen'
  if (s.status === 'writing') return listen ? 'Getting the page ready…' : 'Writing the story…'
  if (s.status === 'narrating') {
    const at = scenes.findIndex(sc => sc.status !== 'done')
    return listen || scenes.length < 2 || at < 0 ? 'Narrating…' : `Narrating scene ${at + 1} of ${scenes.length}…`
  }
  if (s.status === 'failed') return `Failed: ${s.error || 'unknown error'}`
  if (s.status === 'done') return listen ? 'Ready' : `${scenes.length} ${scenes.length === 1 ? 'scene' : 'scenes'}`
  return s.status || ''
}

/* The player is at scene `n` (1-based), which is not ready: it is coming, or it never will. */
export function waitingLabel(n, s) {
  if (s && s.status === 'failed') return `Scene ${n} was not narrated: ${s.error || 'unknown error'}`
  return `Waiting for scene ${n}…`
}

/* A scene's spoken text cut into [{text, part: 'before'|'now'|'after', phrase?}]: the chunk
 * being spoken is 'now'; a highlighted phrase (first occurrence) also carries `phrase`, and
 * stays one phrase (in two parts) when it crosses a chunk edge.
 * `cur` is an index into chunks (chunkAt) or -1; a range that doesn't fit the text is ignored. */
export function sceneSegments(spoken, chunks, cur, phrases = []) {
  const text = spoken || ''
  if (!text) return []
  const c = cur >= 0 && chunks ? chunks[cur] : null
  const ok = c && Number.isInteger(c[0]) && Number.isInteger(c[1]) && c[0] >= 0 && c[1] > c[0] && c[1] <= text.length
  const partAt = (pos) => (!ok ? 'after' : pos < c[0] ? 'before' : pos < c[1] ? 'now' : 'after')
  // phrases are matched on the whole text, so one across a chunk edge is still one phrase;
  // each match is then cut at the chunk edges
  const out = []
  let at = 0
  for (const seg of splitByPhrases(text, phrases, new Set())) {
    const t = typeof seg === 'string' ? seg : seg.text
    const cuts = ok ? [c[0], c[1]].filter(x => x > at && x < at + t.length) : []
    let from = at
    for (const x of [...cuts, at + t.length]) {
      const piece = text.slice(from, x)
      out.push({ text: piece, part: partAt(from), ...(typeof seg === 'string' ? {} : { phrase: seg.phrase }) })
      from = x
    }
    at += t.length
  }
  return out
}

/* Poll story `id` every `interval` ms until it is settled (see isSettled); resolves with the
 * final story, or undefined when `signal` aborts or the story is gone (404). A failed poll goes
 * to onError and polling carries on. Responses after the abort are dropped. */
export function pollStory(id, onUpdate, { interval = 1000, signal, onError = () => {}, fetchJob = storyJob } = {}) {
  return new Promise((resolve) => {
    let timer = null, poller = null
    const finish = (v) => { clearInterval(timer); poller.stop(); signal?.removeEventListener('abort', onAbort); resolve(v) }
    const onAbort = () => finish(undefined)
    poller = createPoller(() => fetchJob(id), (s) => { onUpdate(s); if (isSettled(s)) finish(s) },
      (e) => { onError(e); if (e && e.status === 404) finish(undefined) })
    if (signal?.aborted) return finish(undefined)
    signal?.addEventListener('abort', onAbort)
    poller.tick()
    timer = setInterval(() => poller.tick(), interval)
  })
}
