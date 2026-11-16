/* layout — flowing the bound book's blocks into fixed-proportion pages, pure and DOM-free. The
 * measurer is injected: `measure(html) -> height` (the real one measures in a hidden page-sized
 * frame; tests use characters x line height). Blocks are {kind, html, chapter}.
 *
 * NOTE: layout does not sanitize. The viewer must call sanitize.js `cleanBlocks(blocks)` once,
 * before `layout`: the measurer inserts the HTML it is given, and so does the viewer.
 *
 * Rules:
 *   - the cover and the back cover are alone on their pages;
 *   - a chapter starts on a new page (a chapter-head, or any change of chapter number);
 *   - a block that fits goes on the page;
 *   - a paragraph (or other running text) that does not fit is split at a word boundary
 *     (splitPara), the head ending this page and the tail starting the next; an <ol> continued
 *     on the next page gets `start`, so its numbering carries on;
 *   - a picture, plate, timeline or diagram is never split. One that does not fit is deferred:
 *     it goes to the top of the next page, and the following text of the same chapter fills
 *     the rest of this one. So blocks keep their order, except that a deferred figure lands at
 *     most one page later, never past the next chapter head or another figure. One taller than
 *     a whole page is placed alone with `fit: true` and `scale` (fitScale), drawn scaled down
 *     to the page so the whole block shows;
 *   - keep with next: a heading or chapter-head (or a story heading) never ends a page while its next block goes on
 *     to the next one; it moves too (unless the page would then hold only headings);
 *   - nothing is lost and the loop always advances: an empty page always takes something.
 * Layout can run in chunks: `next` is the state to pass back as `startAt`. */
import { HTML_NS, parseFragment } from './sanitize.js'
import { SVG_NS } from '../apps/diagram.js'

export const PAGE_RATIO = 1.414 // A5, 1 : 1.414
export const FIGURES = new Set(['picture', 'plate', 'timeline', 'diagram'])
const ALONE = new Set(['cover', 'back'])
// running text that may be split to close a page; headings and the rest move whole instead
const RUNNING = new Set(['para', 'list', 'sources', 'contents', 'story', 'closing'])
const HEADINGS = new Set(['heading', 'chapter-head'])
// a `story` block whose root is a heading (the Stories h1, a story's h2, a scene's h3) is a heading
// too: it keeps with its next block and is never split. The kind stays `story`, as the server sends it.
const HEADING_ROOT = /^\s*<h[1-6][\s>]/i
export const isHeadingBlock = (b) => !!b && (HEADINGS.has(String(b.kind || ''))
  || (b.kind === 'story' && HEADING_ROOT.test(String(b.html ?? ''))))

/* ------------------------------------------------------------------ splitting */

const VOID = new Set(['br', 'img', 'hr', 'wbr'])
// never cut inside these: they count as one indivisible unit in the text
const ATOMIC = new Set(['img', 'br', 'svg', 'figure'])
const SPACE = /[ \t\n\r\f]/ // breaking whitespace: not U+00A0
// a cut may also go where one of these starts (list items have no whitespace between them)
const BLOCKS = new Set(['li', 'p', 'h1', 'h2', 'h3', 'h4', 'div', 'section', 'ol', 'ul', 'figcaption'])
const escText = (s) => String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
const escAttr = (s) => escText(s).replace(/"/g, '&quot;')

/* Give each node its [start, end) range in the fragment's text; atomic elements are empty
 * ranges at their position. Returns the whole text and the extra cut points: before an atomic
 * element, and where a block element (a list item, a paragraph) starts. */
function index(root) {
  let text = ''
  const atoms = []
  const starts = []
  const walk = (n) => {
    n.start = text.length
    if (n.nodeType === 3 || n.nodeType === 4) text += n.nodeValue
    else if (n.nodeType === 1 && (n.namespaceURI === SVG_NS || ATOMIC.has(n.localName))) atoms.push(text.length)
    else if (n.nodeType === 1 || n.nodeType === 11) {
      if (n.nodeType === 1 && BLOCKS.has(n.localName)) starts.push(text.length)
      n.childNodes.forEach(walk)
    }
    n.end = text.length
  }
  walk(root)
  return { text, atoms, starts }
}

function writeWhole(n) {
  if (n.nodeType === 3 || n.nodeType === 4) return escText(n.nodeValue)
  if (n.nodeType !== 1) return ''
  return openTag(n, true) + (n.namespaceURI === HTML_NS && VOID.has(n.localName)
    ? '' : n.childNodes.map(writeWhole).join('') + `</${n.localName}>`)
}

// an element's anchor (sanitize writes it as data-anchor; a raw id is treated the same)
const ANCHOR_ATTRS = new Set(['id', 'data-anchor'])

function openTag(n, keepId, extra = []) {
  const names = new Set(extra.map(([k]) => k))
  const attrs = n.attributes.filter((a) => (keepId || !ANCHOR_ATTRS.has(a.name)) && !names.has(a.name))
    .map((a) => [a.name, a.value]).concat(extra)
    .map(([k, v]) => ` ${k}="${escAttr(v)}"`).join('')
  return `<${n.localName}${attrs}>`
}

const attr = (n, name) => (n.attributes.find((a) => a.name === name) || {}).value
const int = (v) => (/^-?\d{1,6}$/.test(String(v ?? '').trim()) ? parseInt(v, 10) : null)

/* The number the first <li> of an <ol> shows from text position `lo` on: the list's start (or
 * 1), stepped item by item, and reset by an item's own value. */
function olStart(n, lo) {
  let k = int(attr(n, 'start')) ?? 1
  for (const c of n.childNodes) {
    if (c.nodeType !== 1 || c.localName !== 'li') continue
    const v = int(attr(c, 'value'))
    if (v !== null) k = v
    if (c.end > lo || (c.start === c.end && c.start >= lo)) return k
    k++
  }
  return k
}

/* The part of `nodes` whose text lies in [lo, hi), with every element it touches opened and
 * closed (balanced). An element begun before `lo` is a continuation: it loses its anchor
 * (data-anchor, or id), so an anchor stays on the page where it starts. */
function writeRange(nodes, lo, hi) {
  let out = ''
  for (const n of nodes) {
    if (n.nodeType === 3 || n.nodeType === 4) {
      const a = Math.max(lo, n.start), b = Math.min(hi, n.end)
      if (a < b) out += escText(n.nodeValue.slice(a - n.start, b - n.start))
    } else if (n.nodeType === 1) {
      if (n.start === n.end) { if (n.start >= lo && n.start < hi) out += writeWhole(n) }
      else if (n.start < hi && n.end > lo) {
        const reopened = n.start < lo
        const extra = reopened && n.localName === 'ol' && n.namespaceURI === HTML_NS
          ? [['start', String(olStart(n, lo))]] : []
        out += openTag(n, !reopened, extra) + writeRange(n.childNodes, lo, hi) + `</${n.localName}>`
      }
    }
  }
  return out
}

/* Split block HTML at a word boundary: [head, tail], where head is the longest prefix (ending
 * after a run of whitespace, before a picture or drawing, or where a list item or other
 * block starts) for which `fits(head)` holds, with inline tags kept balanced and
 * reopened in the tail. head is '' when not even the first word fits; tail is '' when there is
 * no boundary to split at and the whole fits. head's text followed by tail's text is the
 * original text. `fits` is assumed monotone (more text never fits better). */
export function splitPara(html, fits) {
  const root = parseFragment(html)
  const { text, atoms, starts } = index(root)
  // a cut goes after a run of breaking whitespace (never a no-break space), before a picture or
  // drawing, or where a block starts; never where the head would hold only whitespace
  const at = new Set([...atoms, ...starts])
  for (let k = 1; k < text.length; k++) if (SPACE.test(text[k - 1]) && !SPACE.test(text[k])) at.add(k)
  const firstAtom = atoms.length ? Math.min(...atoms) : Infinity
  const firstInk = text.search(/[^ \t\n\r\f]/)
  const inked = (c) => firstAtom < c || (firstInk >= 0 && firstInk < c)
  const cuts = [...at].filter((c) => c > 0 && inked(c)).sort((a, b) => a - b)
  const head = (c) => writeRange(root.childNodes, -Infinity, c)
  let lo = 0, hi = cuts.length - 1, best = -1
  while (lo <= hi) {
    const mid = (lo + hi) >> 1
    if (fits(head(cuts[mid]))) { best = mid; lo = mid + 1 } else hi = mid - 1
  }
  if (best < 0) return ['', html]
  const c = cuts[best]
  return [head(c), writeRange(root.childNodes, c, Infinity)]
}

/* ------------------------------------------------------------------ flowing */

/* The factor that shrinks a block `h` tall to a page `pageHeight` tall: 1 when it fits, else
 * just under pageHeight / h (rounded down, so the scaled block never overflows by a hair). */
export function fitScale(h, pageHeight) {
  const H = Number(h), P = Number(pageHeight)
  if (!(H > 0) || !(P > 0) || H <= P) return 1
  return Math.max(1e-4, Math.floor((P / H) * 1e4) / 1e4)
}

/* Flow `blocks` into pages. Options: measure (required), pageHeight (required, > 0), startAt (a
 * previous call's `next`), chunk (at most this many blocks this call) and shouldYield (() =>
 * true to stop after the current block, e.g. an idle deadline). Returns {pages, done, next}:
 * the pages finished in this call, whether the book is laid out, and the state to continue
 * from (null when done). Pages are {items: [{blockIndex, html, part?, fit?, scale?}], chapter, folio,
 * kind?}, where kind is 'cover' or 'back' on those pages, and folio counts from 1. */
export function layout(blocks, { measure, pageHeight, startAt, chunk = Infinity, shouldYield } = {}) {
  if (typeof measure !== 'function') throw new TypeError('layout needs a measure function')
  if (!(pageHeight > 0)) throw new RangeError('layout needs a positive pageHeight')
  const list = Array.isArray(blocks) ? blocks : []
  const s = startAt || {}
  let i = s.i | 0, tail = s.tail ?? null, part = s.part | 0, used = +s.used || 0, folio = s.folio || 1
  let page = s.page ? { ...s.page, items: [...s.page.items] } : null
  let deferred = s.deferred || null // {blockIndex, html, chapter}: a figure waiting for the next page
  const pages = []
  const heights = new Map()
  const meas = (html) => {
    if (heights.has(html)) return heights.get(html)
    const h = Number(measure(html))
    const v = Number.isFinite(h) && h > 0 ? h : 0
    if (heights.size > 64) heights.clear()
    heights.set(html, v)
    return v
  }
  const kindOf = (k) => String((list[k] && list[k].kind) || '')
  const chapterOf = (k) => { const c = list[k] && list[k].chapter; return Number.isInteger(c) && c > 0 ? c : 0 }
  const open = (chapter, kind) => {
    page = { items: [], chapter, folio }
    if (kind) page.kind = kind
    used = 0
  }
  const place = (blockIndex, html, h, extra = {}) => {
    const item = { blockIndex, html }
    if (extra.part != null) item.part = extra.part
    if (extra.fit) { item.fit = true; item.scale = fitScale(h, pageHeight) }
    page.items.push(item)
    used += extra.fit ? Math.min(h, pageHeight) : h
  }
  /* finish the page; a deferred figure then opens the next one */
  const close = () => {
    if (page && page.items.length) { pages.push(page); folio++ }
    page = null; used = 0
    if (deferred) {
      const d = deferred
      deferred = null
      open(d.chapter)
      const h = meas(d.html)
      place(d.blockIndex, d.html, h, { fit: h > pageHeight })
      if (h > pageHeight) close()
    }
  }
  /* close the page because block i goes on to the next one: trailing headings go with it (keep
   * with next), unless they are all the page holds */
  const breakBefore = () => {
    const items = page ? page.items : []
    let k = items.length
    while (k > 0 && items[k - 1].part == null && isHeadingBlock(list[items[k - 1].blockIndex])) k--
    if (k > 0 && k < items.length) {
      const moved = items.splice(k)
      for (const it of moved) used -= meas(it.html)
      i = moved[0].blockIndex; tail = null; part = 0
      if (deferred && deferred.blockIndex > i) deferred = null // it comes round again, in order
    }
    close()
  }
  let count = 0
  const advance = () => { i++; tail = null; part = 0; count++ }
  /* whether the block after the figure at k is text of the same chapter, to close its gap */
  const canFill = (k) => {
    const n = k + 1
    if (n >= list.length || chapterOf(n) !== chapterOf(k)) return false
    const kn = kindOf(n)
    return !FIGURES.has(kn) && !ALONE.has(kn) && kn !== 'chapter-head'
  }

  while (i < list.length) {
    if (count >= chunk || (count > 0 && shouldYield && shouldYield())) break
    const b = list[i] || {}
    const kind = kindOf(i)
    const chapter = chapterOf(i)
    const html = tail ?? String(b.html ?? '')
    const piece = tail !== null ? part : null // a continuing tail carries its part number

    if (ALONE.has(kind)) {
      while (page || deferred) close()
      open(chapter, kind)
      const h = meas(html)
      place(i, html, h, { fit: h > pageHeight })
      close(); advance()
      continue
    }
    while (page && page.items.length && tail === null && (chapter !== page.chapter || kind === 'chapter-head')) close()
    // a figure waiting for the next page lets only text pass it
    if (deferred && FIGURES.has(kind)) { breakBefore(); continue }
    if (!page) open(chapter)

    const h = meas(html)
    const room = pageHeight - used
    if (h <= room) { place(i, html, h, { part: piece }); advance(); continue }

    const empty = page.items.length === 0
    if (FIGURES.has(kind) && !empty && !deferred && canFill(i)) {
      deferred = { blockIndex: i, html, chapter } // to the top of the next page; text fills this one
      advance()
      continue
    }
    if (!FIGURES.has(kind) && (empty || (RUNNING.has(kind) && !isHeadingBlock(b)))) {
      const [head, rest] = splitPara(html, (x) => meas(x) <= room)
      if (head && rest) {
        place(i, head, meas(head), { part })
        part++; tail = rest
        close()
        continue
      }
    }
    if (!empty) { breakBefore(); continue } // move it whole to the next page
    // alone on a page and still too tall: placed whole, scaled to fit; nothing is lost
    place(i, html, h, { part: piece, fit: true })
    advance(); close()
  }

  const done = i >= list.length
  if (done) while (page || deferred) close()
  const next = done ? null
    : { i, tail, part, used, folio, deferred, page: page ? { ...page, items: [...page.items] } : null }
  return { pages, done, next }
}

/* Every page at once (no chunking). */
export const layoutAll = (blocks, opts) => layout(blocks, { ...opts, startAt: undefined, chunk: Infinity, shouldYield: undefined }).pages

/* ------------------------------------------------------------------ spreads and places */

/* Group pages into spreads of page indices.
 *   - Wide: the covers stand alone, the front cover as {right: k} and the back cover as
 *     {left: k}. Inside pages always pair up as {left, right}; an inside page left over (an odd
 *     run, before the back cover or at the end) gets a blank right partner:
 *     {left: k, right: null, blankRight: true}.
 *   - Narrow: one page per spread, {right: k}, except the back cover, {left: k}. */
export function spreads(pages, { wide = true } = {}) {
  const out = []
  const list = Array.isArray(pages) ? pages : []
  if (!wide) {
    list.forEach((p, k) => out.push(p && p.kind === 'back' ? { left: k } : { right: k }))
    return out
  }
  let left = null
  const leftover = () => {
    if (left !== null) out.push({ left, right: null, blankRight: true })
    left = null
  }
  list.forEach((p, k) => {
    const kind = p && p.kind
    if (kind === 'cover') { leftover(); out.push({ right: k }) }
    else if (kind === 'back') { leftover(); out.push({ left: k }) }
    else if (left === null) left = k
    else { out.push({ left, right: k }); left = null }
  })
  leftover()
  return out
}

/* The index of the spread holding page `pageIndex`, or -1. */
export function spreadOf(list, pageIndex) {
  for (let k = 0; k < list.length; k++) if (list[k].left === pageIndex || list[k].right === pageIndex) return k
  return -1
}

/* The index of the page where block `blockIndex` starts, or -1 when it is not laid out (yet).
 * Each page's first item is its lowest block (a deferred figure heads its page), and those rise
 * page by page; so a binary search finds L, the last page beginning before the block, and the
 * block starts on L or L + 1, or on L - 1 when it is text that closed the gap before a deferred
 * figure heading L. */
export function locate(pages, blockIndex) {
  const list = Array.isArray(pages) ? pages : []
  const first = (k) => (list[k].items.length ? list[k].items[0].blockIndex : -Infinity)
  let lo = 0, hi = list.length - 1, last = 0
  while (lo <= hi) {
    const mid = (lo + hi) >> 1
    if (first(mid) < blockIndex) { last = mid; lo = mid + 1 } else hi = mid - 1
  }
  for (let k = Math.max(0, last - 1); k <= Math.min(list.length - 1, last + 2); k++) {
    if (list[k].items.some((it) => it.blockIndex === blockIndex)) return k
  }
  return -1
}
