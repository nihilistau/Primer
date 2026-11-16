/* view — the bound book viewer's pure parts: the page size and spread mode for a window, the page
 * label and the announcement, chapters for the go-to list and PgUp/PgDn, link routing, the
 * remembered place and settings, and opening a bound window. DOM-free except `storage()`. */
import * as wm from '../windowManager.js'
import { parseFragment } from './sanitize.js'
import { PAGE_RATIO } from './layout.js'

export const WIDE_MIN = 900 // two-page spreads at this width and over, otherwise one page
export const isWide = (width) => Number(width) >= WIDE_MIN

const MARGIN = 20 // around the book on the desk

/* A page's size and type metrics for a stage of `width` x `height` px: A5 pages, two side by
 * side when wide, as large as fit. Everything is whole pixels, so the measurer and the pages
 * agree exactly. contentH is the height layout pours into. */
export function pageMetrics(width, height, wide) {
  const w = Math.max(0, Number(width) || 0), h = Math.max(0, Number(height) || 0)
  const availW = (wide ? w / 2 : w) - MARGIN * (wide ? 1 : 2)
  const availH = h - MARGIN * 2
  const pw = Math.max(160, Math.floor(Math.min(availW, availH / PAGE_RATIO)))
  const ph = Math.round(pw * PAGE_RATIO)
  const fs = Math.max(10, Math.min(19, Math.round(pw / 30)))
  const padX = Math.round(pw * 0.09)
  const padT = Math.round(ph * 0.075)
  const padB = Math.round(ph * 0.085)
  return { pw, ph, fs, padX, padT, padB, contentW: pw - 2 * padX, contentH: ph - padT - padB }
}

/* the pages a spread shows: wide, its left and right (null for none); narrow, one page */
export function sides(spread, wide) {
  if (!spread) return { left: null, right: null }
  if (!wide) return { left: null, right: spread.right ?? spread.left ?? null }
  return { left: spread.left ?? null, right: spread.right ?? null, blankRight: !!spread.blankRight }
}

const pagesOf = (spread) => (spread ? [spread.left, spread.right].filter((k) => k !== null && k !== undefined) : [])

/* "Pages 10–11 of 48", "Page 1 of 48", or "of about 40" while the layout runs */
export function pageLabel(spread, total, done = true) {
  const ks = pagesOf(spread).sort((a, b) => a - b)
  const of = `of ${done ? '' : 'about '}${Math.max(total | 0, ks.length ? ks[ks.length - 1] + 1 : 0)}`
  if (!ks.length) return ''
  return ks.length > 1 ? `Pages ${ks[0] + 1}–${ks[1] + 1} ${of}` : `Page ${ks[0] + 1} ${of}`
}

/* what turning says through the live region: "Pages 10–11 of 48, Chapter: Bodiam Castle" */
export const announcement = (label, chapterTitle) => (chapterTitle ? `${label}, Chapter: ${chapterTitle}` : label)

/* while the layout runs: an estimate of the page count from the pages made so far */
export function aboutTotal(pagesMade, blocksDone, blocksTotal) {
  if (!(blocksDone > 0) || blocksDone >= blocksTotal) return pagesMade
  return Math.max(pagesMade, Math.round((pagesMade * blocksTotal) / blocksDone))
}

const textOf = (n) => (n.nodeType === 3 || n.nodeType === 4 ? n.nodeValue : (n.childNodes || []).map(textOf).join(''))
function find(n, name) {
  if (n.nodeType === 1 && n.localName === name) return n
  for (const c of n.childNodes || []) { const f = find(c, name); if (f) return f }
  return null
}

/* the chapters, in order: [{chapter, title, blockIndex, nodeId}] from the chapter-head blocks */
export function chapterList(blocks) {
  const out = []
  ;(blocks || []).forEach((b, k) => {
    if (!b || b.kind !== 'chapter-head') return
    const h = find(parseFragment(b.html), 'h1')
    out.push({ chapter: b.chapter, title: (h ? textOf(h) : '').replace(/\s+/g, ' ').trim(), blockIndex: k, nodeId: b.node_id || '' })
  })
  return out
}

/* the chapter a spread is in: its right page's (or only page's) */
export function spreadChapter(spread, pages) {
  const ks = pagesOf(spread)
  const p = ks.length ? pages[ks[ks.length - 1]] : null
  return p ? p.chapter : 0
}

/* PgUp/PgDn: the spread where the previous or next chapter (or part) starts. A chapter starts
 * on a page whose chapter differs from the page before. Going back from inside a chapter goes
 * to its start; from its start, to the one before. Returns the spread index, or -1. */
export function chapterSpread(list, pages, at, dir) {
  const cur = pagesOf(list[at])
  if (!cur.length) return -1
  const lo = Math.min(...cur), hi = Math.max(...cur)
  const starts = []
  pages.forEach((p, k) => { if (k === 0 || p.chapter !== pages[k - 1].chapter || p.kind || pages[k - 1].kind) starts.push(k) })
  const target = dir > 0 ? starts.find((k) => k > hi) : [...starts].reverse().find((k) => k < lo)
  if (target === undefined) return -1
  for (let s = 0; s < list.length; s++) if (list[s].left === target || list[s].right === target) return s
  return -1
}

/* ------------------------------------------------------------------ links */

const ANCHOR = /^#([A-Za-z][\w-]{0,80})$/
const EXTERNAL = /^https?:\/\/[^\s"'<>\\`\x00-\x1f\x7f]+$/i

/* The page holding the element anchored `id` (its data-anchor, as sanitize writes a block's ids),
 * or -1 (not laid out yet, or none). A split block keeps its anchor on the part where the element
 * starts. `#cN` falls back to chapter N's first page. */
export function anchorPage(pages, id) {
  if (!ANCHOR.test('#' + id)) return -1
  const needle = new RegExp(`\\sdata-anchor="${id}"`)
  for (let k = 0; k < pages.length; k++) if (pages[k].items.some((it) => needle.test(it.html))) return k
  const m = /^c(\d+)$/.exec(id)
  if (m) { const n = +m[1]; const k = pages.findIndex((p) => p.chapter === n); if (k >= 0) return k }
  return -1
}

/* Where a click on a link inside the book goes:
 *   {go: 'page', page}     an anchor laid out on that page (a contents entry, a citation);
 *   {go: 'wait', id}       an anchor not laid out yet (the layout is still running);
 *   {go: 'browser', url}   an http(s) link: ask first, then Primer's Browser window;
 *   {go: 'none'}           anything else, which does nothing. */
export function routeLink(href, pages = [], done = true) {
  const h = typeof href === 'string' ? href.trim() : ''
  const a = ANCHOR.exec(h)
  if (a) {
    const page = anchorPage(pages, a[1])
    if (page >= 0) return { go: 'page', page }
    return done ? { go: 'none' } : { go: 'wait', id: a[1] }
  }
  if (EXTERNAL.test(h)) return { go: 'browser', url: h }
  return { go: 'none' }
}

/* A click, middle-click (auxclick) or drag on a link inside the book. Its default is always
 * prevented, so no link ever opens a new tab or navigates Primer away; a click or a middle-click
 * is routed through `follow(href)` (other buttons only lose the default). Returns whether the
 * event was on a link. */
export function linkEvent(e, follow) {
  const a = e && e.target && typeof e.target.closest === 'function' ? e.target.closest('a') : null
  if (!a) return false
  e.preventDefault()
  if (e.type === 'click' || (e.type === 'auxclick' && e.button === 1)) follow(a.getAttribute('href'))
  return true
}

/* Which way a click at x turns: the outer third of the book's outer pages, +1 (forward) on the
 * right and -1 on the left, else 0. rect is the book's {left, right, width}. */
export function clickZone(x, rect, wide) {
  if (!rect || !(rect.width > 0)) return 0
  const pw = wide ? rect.width / 2 : rect.width
  if (x > rect.right - pw / 3) return 1
  if (x < rect.left + pw / 3) return -1
  return 0
}

/* the first picture on a page, for its thumbnail (only Primer's own media URLs) */
export function pagePicture(page) {
  for (const it of (page && page.items) || []) {
    const m = /<img[^>]*\ssrc="(\/v1\/media\/[0-9a-f]{64}\.png)"/.exec(it.html)
    if (m) return m[1]
  }
  return ''
}

/* ------------------------------------------------------------------ remembered */

/* localStorage, or null where it can't be had (every access is wrapped) */
export function storage() {
  try { return typeof localStorage !== 'undefined' ? localStorage : null } catch { return null }
}

export const PAPER_KEY = 'primer.bound.paper'
export const SOUND_KEY = 'primer.bound.sound'

export function readPref(store, key, fallback) {
  try {
    const v = store ? store.getItem(key) : null
    return v === null || v === undefined ? fallback : v
  } catch { return fallback }
}

export function writePref(store, key, value) {
  try { if (store) store.setItem(key, String(value)) } catch { /* full or blocked: not remembered */ }
}

export const readPaper = (store) => (readPref(store, PAPER_KEY, 'night') === 'day' ? 'day' : 'night')
export const readSound = (store) => readPref(store, SOUND_KEY, 'off') === 'on'

/* the place's key: per book id, or per scope and include switches for the live trail (a switch
 * turned on or off moves every block index after it, so a place is only kept for the same ones) */
const sorted = (o) => (o && typeof o === 'object' ? Object.keys(o).sort().map((k) => [k, o[k]]) : [])
export function placeKey({ bookId, scope, include } = {}) {
  if (bookId) return `primer.bound.place.book:${bookId}`
  if (scope && typeof scope === 'object') {
    return 'primer.bound.place.trail:' + JSON.stringify([sorted(scope), sorted(include)])
  }
  return ''
}

/* the remembered place: {block} (the first block on the page read last), or null */
export function readPlace(store, key) {
  if (!key) return null
  try {
    const v = JSON.parse(readPref(store, key, 'null'))
    return v && Number.isInteger(v.block) && v.block >= 0 ? { block: v.block } : null
  } catch { return null }
}

export function writePlace(store, key, block) {
  if (key && Number.isInteger(block) && block >= 0) writePref(store, key, JSON.stringify({ block }))
}

/* ------------------------------------------------------------------ opening */

export const BOUND_SIZE = [1040, 760]

/* Open a bound book window. props: {bookId} for a made book, or {scope, include, title?, by?}
 * for the live trail; chapter (from 1) opens it at that chapter. Returns the window id. */
export const openBound = (props) => wm.open('bound', { props, w: BOUND_SIZE[0], h: BOUND_SIZE[1] })
