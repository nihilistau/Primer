/* book — the Book window's words and request bodies (Phase 13). The server collects the trail
 * into chapters and writes the EPUB and the HTML; this file only says what to ask for and how
 * to show the answers. Scopes mirror primer/book/model.py: {kind: "all"}, {kind: "branch",
 * node_id} or {kind: "since", since: <unix seconds>}. */

export const MAX_TITLE = 120
export const MAX_BY = 80
export const INCLUDE_KEYS = ['pictures', 'maps', 'timelines', 'diagrams', 'stories']
export const INCLUDE_LABELS = { pictures: 'Pictures', maps: 'Maps', timelines: 'Timelines', diagrams: 'Diagrams', stories: 'Stories' }
export const allIncluded = () => Object.fromEntries(INCLUDE_KEYS.map(k => [k, true]))
const MAX_SINCE = 4102444800   // 1 January 2100, as the server's MAX_SINCE
const MONTHS = ['January', 'February', 'March', 'April', 'May', 'June', 'July', 'August', 'September',
                'October', 'November', 'December']

/* The pages a branch book can start from: GET /v1/books/pages ({pages: [{id, title, concept}]},
 * the pages with a version, so read ones only); a {nodes} graph snapshot is read the same way.
 * Filtered by `query` (case-insensitive, in the title) and sorted by title, case-insensitively.
 * A title shared by more than one page carries its concept as `note` when that says more. */
export function pickablePages(data, query = '') {
  const list = data && (Array.isArray(data.pages) ? data.pages : Array.isArray(data.nodes) ? data.nodes : [])
  const seen = new Set(), out = []
  for (const n of list || []) {
    if (!n || typeof n.id !== 'string' || !n.id || seen.has(n.id)) continue
    const title = typeof n.title === 'string' ? n.title.trim() : ''
    if (!title) continue
    seen.add(n.id)
    out.push({ id: n.id, title, concept: typeof n.concept === 'string' ? n.concept.trim() : '' })
  }
  const count = new Map()
  for (const p of out) { const k = p.title.toLowerCase(); count.set(k, (count.get(k) || 0) + 1) }
  const q = String(query || '').trim().toLowerCase()
  return out
    .filter(p => !q || p.title.toLowerCase().includes(q))
    .sort((a, b) => {
      const x = a.title.toLowerCase(), y = b.title.toLowerCase()
      return x < y ? -1 : x > y ? 1 : (a.id < b.id ? -1 : a.id > b.id ? 1 : 0)
    })
    .map(p => {
      const dup = count.get(p.title.toLowerCase()) > 1
      const note = dup && p.concept && p.concept.toLowerCase() !== p.title.toLowerCase() ? p.concept : ''
      return note ? { id: p.id, title: p.title, note } : { id: p.id, title: p.title }
    })
}

/* "10 November 2026" for a Date, in local time (as the server's time.localtime) */
export const longDate = (d) => `${d.getDate()} ${MONTHS[d.getMonth()]} ${d.getFullYear()}`

/* The server's default_title: "My wanderings", the branch page's title, or "Since 9 November
 * 2026" (the since time's local date; `date`, a Date, when given, else scope.since). At most
 * 120 characters, as the server cuts it. */
export function defaultTitle(scope, pageTitle = '', date) {
  const kind = scope && scope.kind
  let t = 'My wanderings'
  if (kind === 'branch') t = String(pageTitle || '')
  else if (kind === 'since') {
    const d = date instanceof Date ? date : (scope && Number.isFinite(scope.since) ? new Date(scope.since * 1000) : null)
    t = d && !Number.isNaN(d.getTime()) ? `Since ${longDate(d)}` : ''
  }
  return t.slice(0, MAX_TITLE)
}

/* "3.2 MB": 1024-based, one decimal under 10, whole above */
export function formatSize(bytes) {
  const n = Number(bytes)
  if (!Number.isFinite(n) || n < 0) return ''
  if (n < 1024) return `${Math.round(n)} bytes`
  const units = ['KB', 'MB', 'GB', 'TB']
  let v = n / 1024, i = 0
  while (v >= 1024 && i < units.length - 1) { v /= 1024; i++ }
  const s = v < 10 ? (Math.round(v * 10) / 10).toFixed(1) : String(Math.round(v))
  // 9.96 rounds to "10.0": say "10" instead
  return `${s === '10.0' ? '10' : s} ${units[i]}`
}

/* a <input type=date> value ("2026-11-09") as local midnight in unix seconds, or null */
export function sinceSeconds(value) {
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(String(value || ''))
  if (!m) return null
  const [y, mo, d] = [Number(m[1]), Number(m[2]), Number(m[3])]
  const date = new Date(y, mo - 1, d)
  if (date.getFullYear() !== y || date.getMonth() !== mo - 1 || date.getDate() !== d) return null
  const s = Math.floor(date.getTime() / 1000)
  return s >= 0 && s <= MAX_SINCE ? s : null
}

/* the scope for the window's state, or null while it is incomplete (no page, no date) */
export function scopeOf(state) {
  const kind = state && state.kind
  if (kind === 'all') return { kind: 'all' }
  if (kind === 'branch') return state.nodeId ? { kind: 'branch', node_id: state.nodeId } : null
  if (kind === 'since') {
    const s = sinceSeconds(state.since)
    return s === null ? null : { kind: 'since', since: s }
  }
  return null
}

/* The POST /v1/books body {scope, include, title, by} for the window's state {kind, nodeId?,
 * since? ("YYYY-MM-DD"), include, title, by}, or null while the scope is incomplete. A blank
 * title is sent as "" (the server uses its default); title and By are trimmed and cut to their
 * limits. */
export function scopeBody(state) {
  const scope = scopeOf(state)
  if (!scope) return null
  const inc = (state && state.include) || {}
  const include = Object.fromEntries(INCLUDE_KEYS.map(k => [k, inc[k] !== false]))
  return {
    scope, include,
    title: String((state && state.title) || '').trim().slice(0, MAX_TITLE),
    by: String((state && state.by) || '').trim().slice(0, MAX_BY),
  }
}

const plural = (n, one, many) => `${n} ${n === 1 ? one : many}`

/* "40 chapters, 52 pictures, about 81 MB" for a POST /v1/books/preview answer */
export function previewText(p) {
  if (!p) return ''
  const chapters = Number(p.chapters) || 0, pictures = Number(p.pictures) || 0
  if (!chapters) return 'Nothing to put in the book: no pages read in that part of the trail.'
  return `${plural(chapters, 'chapter', 'chapters')}, ${plural(pictures, 'picture', 'pictures')}, about ${formatSize(p.bytes)}`
}

export const BOOK_ENDED = new Set(['done', 'failed'])
export const isActive = (b) => !!b && (b.status === 'queued' || b.status === 'making')

/* what a book row from GET /v1/books/<id> says: "EPUB: chapter 12 of 40" while making */
export function progressText(b) {
  if (!b) return ''
  if (b.status === 'queued') return 'Waiting to start…'
  if (b.status === 'making') {
    const p = b.progress
    if (!p || !p.total) return 'Starting…'
    const stage = p.stage === 'html' ? 'HTML' : 'EPUB'
    return `${stage}: chapter ${Math.min(p.done || 0, p.total)} of ${p.total}`
  }
  if (b.status === 'done') return 'Done'
  if (b.status === 'failed') return `Failed: ${b.error || 'unknown error'}`
  return String(b.status || '')
}

/* "EPUB 3.2 MB · HTML 4.1 MB" for a finished book */
export function sizesText(b) {
  if (!b) return ''
  const parts = []
  if (Number.isFinite(b.epub_size)) parts.push(`EPUB ${formatSize(b.epub_size)}`)
  if (Number.isFinite(b.html_size)) parts.push(`HTML ${formatSize(b.html_size)}`)
  return parts.join(' · ')
}

/* a book row's created_at (unix seconds) as "10 November 2026, 14:05" */
export function bookDate(ts) {
  if (!Number.isFinite(ts)) return ''
  const d = new Date(ts * 1000)
  const hh = String(d.getHours()).padStart(2, '0'), mm = String(d.getMinutes()).padStart(2, '0')
  return `${longDate(d)}, ${hh}:${mm}`
}

/* the error a failed request should show: the server's reason (413 "Too big: …", 409 "A book
 * is being made…", 404 "that page was never read") as it said it; a request that got no HTTP
 * answer (the browser's own "Failed to fetch") reads "Could not reach Primer." */
export function bookErrorText(e) {
  if (!e || !e.status) return 'Could not reach Primer.'
  const m = e.message || ''
  if (e.status === 404 && /never read|unknown page/.test(m)) {
    return m === 'unknown page' ? 'That page is not in the trail any more.' : 'That page was never read, so it has no chapter.'
  }
  return m || `Primer answered ${e.status}.`
}
