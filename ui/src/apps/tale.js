/* tale — what the Tale's state means to the Tale window, the dock and the Story window. Pure. */
import { quizErrorText } from './readerModel.js'

export const SEED_MAX = 500       // the reader's seed (the server refuses more)
export const MAX_PAGES = 3        // pages a chapter draws on
export const MARK_AT = 3          // the dock's mark: this many unused pages wait
export const TRAIL_LIMIT = 12     // pages offered by "Add a page from your trail"

/* the Tale window and the dock re-read GET /v1/tales after this event */
export const TALE_EVENT = 'primer:tale'
export const announceTale = () => { if (typeof window !== 'undefined') window.dispatchEvent(new Event(TALE_EVENT)) }

const waitingOf = (preview) => (preview && Number.isFinite(preview.waiting) ? preview.waiting : 0)

export function previewLabel(preview) {
  const n = waitingOf(preview)
  if (n <= 0) return 'No new pages since the last chapter'
  const words = `${n} new ${n === 1 ? 'page' : 'pages'} waiting`
  return n >= MARK_AT ? `${words}: time for the next chapter` : words
}

/* the dock's mark: the count when enough pages wait, else 0 (no mark) */
export const waitingMark = (preview) => (waitingOf(preview) >= MARK_AT ? waitingOf(preview) : 0)
export const waitingMarkLabel = (n) => `${n} pages waiting for the next chapter`

const STATUS_WORDS = { writing: 'being written', narrating: 'being narrated', done: 'ready', failed: 'failed' }

/* the chapter's own title without a leading "Chapter N:" (the server names chapters that way) */
function bareTitle(n, title) {
  const t = String(title || '').trim()
  const m = t.match(/^Chapter\s+\d+\s*:?\s*(.*)$/i)
  return m ? m[1].trim() : t
}

export function chapterLabel(ch) {
  const parts = [`Chapter ${ch.n}`]
  const t = bareTitle(ch.n, ch.title)
  if (t) parts.push(t)
  if (ch.status) parts.push(STATUS_WORDS[ch.status] || ch.status)
  return parts.join(' · ')
}

/* the Story window's heading for a chapter: "Chapter N of <tale>" when the tale is known
 * (info {n, taleTitle}), else "Chapter" with the story's title */
export function chapterHeading(info, title) {
  if (info && Number.isFinite(info.n) && info.taleTitle) return `Chapter ${info.n} of ${info.taleTitle}`
  const t = String(title || '').trim()
  if (/^Chapter\b/i.test(t)) return t
  if (info && Number.isFinite(info.n)) return t ? `Chapter ${info.n}: ${t}` : `Chapter ${info.n}`
  return t ? `Chapter: ${t}` : 'Chapter'
}

/* the chapter told by story `storyId` in the current tale, as {n, taleTitle}, or null */
export function findChapter(state, storyId) {
  const cur = state && state.current
  if (!cur || !Array.isArray(cur.chapters)) return null
  const ch = cur.chapters.find(c => c && c.story_id === storyId)
  return ch ? { n: ch.n, taleTitle: cur.title || '' } : null
}

const list = (x) => (Array.isArray(x) ? x : [])
const str = (x) => (typeof x === 'string' ? x : '')

/* the bible's lists as sections of plain-text items; places and threads can be removed */
export function bibleRows(bible) {
  const b = bible || {}
  const companions = list(b.companions).filter(c => c && typeof c === 'object' && str(c.name))
    .map(c => ({ text: c.name, detail: [str(c.look), str(c.note)].filter(Boolean).join(' · ') }))
  const places = list(b.places).filter(p => str(p))
    .map(p => ({ text: p, detail: '', remove: { remove_place: p } }))
  const things = list(b.things).filter(t => str(t)).map(t => ({ text: t, detail: '' }))
  const threads = list(b.threads).filter(t => t && typeof t === 'object' && str(t.text))
    .map(t => ({ text: t.text, detail: t.open === false ? 'closed' : 'open', remove: { remove_thread: t.text } }))
  return [
    { key: 'companions', label: 'Companions', items: companions },
    { key: 'places', label: 'Places', items: places },
    { key: 'things', label: 'Things carried', items: things },
    { key: 'threads', label: 'Threads', items: threads },
  ]
}

/* can Next chapter be pressed with these pages chosen? */
export function canNext(state, chosen) {
  const cur = state && state.current
  if (!cur) return { ok: false, reason: 'Start a tale first' }
  if (cur.writing) return { ok: false, reason: 'A chapter is being written' }
  if (cur.updating) return { ok: false, reason: 'The story bible is being updated' }
  const n = list(chosen).length
  if (!n) return { ok: false, reason: 'Read a page or two first, or add one from your trail' }
  if (n > MAX_PAGES) return { ok: false, reason: `Choose up to ${MAX_PAGES} pages` }
  return { ok: true, reason: '' }
}

/* what the tale is busy with ('' when nothing) */
export function progressLabel(cur) {
  if (!cur) return ''
  if (cur.writing) return `Writing chapter ${list(cur.chapters).length + 1}…`
  if (cur.updating) return 'Updating the story bible…'
  return ''
}

/* every suggested page starts ticked */
export const defaultChosen = (preview) => list(preview && preview.pages).map(p => p.node_id)

export function seedCheck(text) {
  const t = String(text || '')
  const count = `${t.length} / ${SEED_MAX}`
  if (!t.trim()) return { ok: false, count, reason: 'Say who the story is about, or let Primer invent one' }
  if (t.length > SEED_MAX) return { ok: false, count, reason: `At most ${SEED_MAX} characters` }
  return { ok: true, count, reason: '' }
}

/* pages recently arrived at (GET /v1/graph: the newest edge into each), newest first, skipping
 * `exclude` node ids and edges to unknown nodes */
export function trailPages(graph, exclude = [], limit = TRAIL_LIMIT) {
  if (!graph) return []
  const nodes = new Map(list(graph.nodes).map(n => [n.id, n]))
  const skip = new Set(exclude)
  const edges = list(graph.edges).slice().sort((a, b) => (b.at || 0) - (a.at || 0))
  const out = []
  for (const e of edges) {
    if (out.length >= limit) break
    const n = nodes.get(e.to_id)
    if (!n || skip.has(n.id)) continue
    skip.add(n.id)
    out.push({ node_id: n.id, title: n.title || n.concept || n.id })
  }
  return out
}

/* a Tale request's failure: the server's own words, an unreadable reply renamed */
export function taleErrorText(e) {
  if (e && e.status === 502) return quizErrorText(e)
  return String((e && e.message) || e)
}

/* why the bible can't be edited now ('' when it can): a chapter or an update would race it */
export function bibleLock(cur) {
  if (cur && cur.writing) return 'A chapter is being written'
  if (cur && cur.updating) return 'The story bible is being updated'
  return ''
}

/* how often the Tale window re-reads GET /v1/tales: quickly while a chapter is written or the
 * bible updated, slowly while a chapter's story is still being written or narrated (so its row
 * moves on to "ready"), not at all otherwise */
export const POLL_BUSY_MS = 1500
export const POLL_TELLING_MS = 5000
export function pollMs(cur) {
  if (!cur) return 0
  if (cur.writing || cur.updating) return POLL_BUSY_MS
  const telling = list(cur.chapters).some(c => c && (c.status === 'writing' || c.status === 'narrating'))
  return telling ? POLL_TELLING_MS : 0
}

/* the hero's gender in words ("Female", "Male"), '' when unset */
export function heroGender(tale) {
  const g = tale && tale.bible && tale.bible.hero && tale.bible.hero.gender
  return g === 'female' ? 'Female' : g === 'male' ? 'Male' : ''
}

/* the hero's name, else the tale's title */
export function heroName(tale) {
  if (!tale) return ''
  const hero = tale.bible && tale.bible.hero
  const name = hero && typeof hero.name === 'string' ? hero.name.trim() : ''
  return name || tale.title || ''
}

/* a story's own `tale` field ({id, title, n}; null when superseded) as chapter info, or null */
export function storyChapter(story) {
  const t = story && story.tale
  return t && Number.isFinite(t.n) ? { n: t.n, taleTitle: t.title || '' } : null
}
