/* BoundBook — the bound book (Phase 16): a book drawn in 3D on the desk, read by turning its pages.
 * Props: { windowId, bookId } for a made book, or { windowId, scope, include, title?, by? } for the
 * live trail; `chapter` (from 1) opens it at that chapter.
 *
 * - The blocks come from GET /v1/books/<id>/blocks or POST /v1/books/blocks. They are checked by
 *   sanitize.js `cleanBlocks` once, before anything measures or inserts them.
 * - Pages are poured in the browser (layout.js) in chunks on idle time, measured in a hidden
 *   page-sized frame with the same CSS as the pages; re-laid out when the window is resized,
 *   keeping the reader at the same block.
 * - The book is CSS 3D: a leaf turns about the spine with rotateY, two faces each with
 *   backface-visibility hidden, drawn as STRIPS strips that bend more toward the free edge, with a
 *   fold gradient and a cast shadow (turn.js). With reduced motion, spreads cross-fade instead.
 * - Only the open spread (and, mid-turn, the leaf and the spread beneath) is in the DOM. */
import { memo, useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'
import * as api from '../api.js'
import * as wm from '../windowManager.js'
import { openBrowser } from '../openBrowser.js'
import { browseAddressFor } from '../apps/browserNav.js'
import { bookErrorText } from '../apps/book.js'
import { Button } from '../kit/parts.jsx'
import { cleanBlocks } from './sanitize.js'
import { layout, locate, spreadOf, spreads } from './layout.js'
import * as T from './turn.js'
import { createTurnSound } from './sound.js'
import {
  PAPER_KEY, SOUND_KEY, aboutTotal, anchorPage, clickZone, linkEvent, announcement, chapterList, chapterSpread, isWide, pageLabel, pageMetrics,
  pagePicture, placeKey, readPaper, readPlace, readSound, roveCurrent, roveNext, routeLink, sides, spreadChapter, storage,
  writePlace, writePref,
} from './view.js'
import './bound.css'

export const REBUILT_NOTE = 'Rebuilt from your trail as it is now'
export const ASK_BROWSER = 'Open in the Browser?'
export const OPEN_IN_PRIMER = 'Open this page in Primer'

const CLOSED_TILT = { x: 24, z: -6, scale: 0.12 } // the closed book lies angled on the desk
const BAR_H = 52
const PAGE_KEYS = new Set(['ArrowRight', 'ArrowLeft', 'Home', 'End', 'PageDown', 'PageUp'])
const SELECT_MS = 400 // a second click this soon after a click turned a leaf selects a word instead

const reducedMotion = () => {
  try { return !!(window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches) } catch { return false }
}

/* the serif the pages are set in, loaded before anything is measured (never waiting long) */
function fontsReady() {
  try {
    const f = typeof document !== 'undefined' && document.fonts
    if (!f) return Promise.resolve()
    const loads = Promise.all([f.load('16px "Source Serif 4 Variable"'), f.load('italic 16px "Source Serif 4 Variable"')]).catch(() => {})
    return Promise.race([loads.then(() => f.ready), new Promise((r) => setTimeout(r, 1500))]).catch(() => {})
  } catch { return Promise.resolve() }
}

const schedule = (fn, soon) => (!soon && typeof requestIdleCallback === 'function'
  ? { idle: requestIdleCallback(fn, { timeout: 150 }) } : { t: setTimeout(() => fn(null), soon ? 0 : 16) })
const unschedule = (h) => {
  if (!h) return
  if (h.idle !== undefined) { if (typeof cancelIdleCallback === 'function') cancelIdleCallback(h.idle) } else clearTimeout(h.t)
}

const cssVars = (m) => ({
  '--pw': m.pw + 'px', '--ph': m.ph + 'px', '--fs': m.fs + 'px', '--pad-x': m.padX + 'px', '--pad-t': m.padT + 'px',
  '--pad-b': m.padB + 'px', '--content-h': m.contentH + 'px', '--content-w': m.contentW + 'px',
})

const shiftOf = (sp, wide, pw) => {
  if (!wide || !sp) return 0
  if (sp.left == null && sp.right != null) return -pw / 2
  if (sp.right == null && sp.left != null && !sp.blankRight) return pw / 2
  return 0
}

/* One page. A hidden copy (on a turning leaf) is inert and unlabelled. */
const PageView = memo(function PageView({ page, index, side, bookTitle, chapterTitle, kindOf, nodeOf, hidden, onOpenNode }) {
  if (!page) return <div className={`bb-page bb-${side} bb-blank`} aria-hidden="true" />
  const first = page.items[0]
  const opensChapter = !!first && first.part == null && kindOf(first.blockIndex) === 'chapter-head'
  const nodeId = opensChapter ? nodeOf(first.blockIndex) : ''
  const head = page.kind || opensChapter ? '' : side === 'left' ? bookTitle : (chapterTitle || bookTitle)
  const a11y = hidden ? { 'aria-hidden': true, inert: '' } : { role: 'region', 'aria-label': `Page ${index + 1}`, tabIndex: -1 }
  return (
    <section className={`bb-page bb-${side}${page.kind ? ' bb-' + page.kind : ''}`} {...a11y}>
      {head ? <div className="bb-head">{head}</div> : null}
      {nodeId ? (hidden ? <span className="bb-open">{OPEN_IN_PRIMER}</span>
        : <button type="button" className="bb-open" onClick={() => onOpenNode(nodeId)}>{OPEN_IN_PRIMER}</button>) : null}
      <div className="bb-content">
        {page.items.map((it, k) => (it.fit ? (
          // taller than a page and not split: drawn as measured, then scaled down to the page whole
          <div key={k} className="bb-fit">
            <div className="bb-item bb-scaled" style={{ transform: `scale(${it.scale || 1})` }} dangerouslySetInnerHTML={{ __html: it.html }} />
          </div>
        ) : (
          <div key={k} className="bb-item" dangerouslySetInnerHTML={{ __html: it.html }} />
        )))}
      </div>
      {page.kind ? null : <div className="bb-folio">{index + 1}</div>}
    </section>
  )
})

/* the leaf's two faces. Their class names must never be a page's `bb-<kind>` (a page of kind
 * 'back' is the back cover): the back face is turned over with rotateY(180deg), and a page sharing
 * its class would be drawn mirrored. */
export const FACE_FRONT = 'bb-face bb-face-front'
export const FACE_BACK = 'bb-face bb-face-back'

const grad = (a, b) => `linear-gradient(to right, rgba(0,0,0,${a.toFixed(3)}), rgba(0,0,0,${b.toFixed(3)}))`

/* the turning leaf: STRIPS strips, each hinged to the one before, both faces sliced from a page */
function Leaf({ turning, front, back, pw, wide }) {
  const p = T.shown(turning)
  const { base, deltas, angles } = T.curl(p, turning.dir)
  const sw = pw / T.STRIPS
  let node = null
  for (let i = T.STRIPS - 1; i >= 0; i--) {
    const a0 = T.shade(angles[i]), a1 = T.shade(angles[Math.min(i + 1, T.STRIPS - 1)])
    node = (
      <div className="bb-strip" style={{ left: i === 0 ? 0 : sw, width: sw, transform: `rotateY(${i === 0 ? base : deltas[i]}deg)` }}>
        <div className={FACE_FRONT}>
          <div className="bb-slice" style={{ left: -i * sw }}>{front}</div>
          <div className="bb-shade" style={{ background: grad(a0, a1) }} />
        </div>
        <div className={FACE_BACK}>
          <div className="bb-slice" style={{ left: -(pw - (i + 1) * sw) }}>{back}</div>
          <div className="bb-shade" style={{ background: grad(a1, a0) }} />
        </div>
        {node}
      </div>
    )
  }
  return <div className="bb-leaf" style={{ left: wide ? pw : 0 }} aria-hidden="true">{node}</div>
}

/* the shadow the lifted leaf casts on the page beneath it */
function Cast({ turning, pw, wide }) {
  const p = T.shown(turning)
  const { base } = T.curl(p, turning.dir)
  const reach = pw * Math.cos((base * Math.PI) / 180) // where the leaf's edge is, from the spine
  const strength = 0.55 * Math.sin(Math.PI * p)
  const right = reach >= 0
  const w = Math.min(pw, Math.abs(reach) + 36)
  if (!wide && !right) return null
  return (
    <div className="bb-cast" aria-hidden="true" style={{
      left: right ? (wide ? pw : 0) : pw - w, width: w,
      background: right
        ? `linear-gradient(to right, rgba(0,0,0,${(strength * 0.5).toFixed(3)}), rgba(0,0,0,${strength.toFixed(3)}) calc(100% - 36px), rgba(0,0,0,0))`
        : `linear-gradient(to left, rgba(0,0,0,${(strength * 0.5).toFixed(3)}), rgba(0,0,0,${strength.toFixed(3)}) calc(100% - 36px), rgba(0,0,0,0))`,
    }} />
  )
}

const Eye = () => (
  <svg width="16" height="16" viewBox="0 0 24 24" aria-hidden="true" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round">
    <path d="M2.5 12s3.5-6.5 9.5-6.5S21.5 12 21.5 12s-3.5 6.5-9.5 6.5S2.5 12 2.5 12z" /><circle cx="12" cy="12" r="2.8" />
  </svg>
)

export default function BoundBook({ windowId, bookId, scope, include, title, by, chapter }) {
  const store = useMemo(() => storage(), [])
  const key = placeKey({ bookId, scope, include })
  const [data, setData] = useState(null)        // {blocks, title, rebuilt}
  const [err, setErr] = useState('')
  const [attempt, setAttempt] = useState(0)
  const [box, setBox] = useState(null)          // the stage's size, px
  const [lay, setLay] = useState({ pages: [], done: false, i: 0, m: null, wide: false })
  const [ts, setTs] = useState(T.idle(0))
  const [fade, setFade] = useState(0)
  const [paper, setPaper] = useState(() => readPaper(store))
  const [soundOn, setSoundOn] = useState(() => readSound(store))
  const [pop, setPop] = useState('')            // '', 'goto' or 'thumbs'
  const [ask, setAsk] = useState('')            // the external address waiting for "Open in the Browser?"
  const [live, setLive] = useState('')
  const [reduced, setReduced] = useState(reducedMotion)
  const rootRef = useRef(null)
  const stageRef = useRef(null)
  const bookRef = useRef(null)
  const itemRef = useRef(null)
  const goal = useRef(null)                     // {block} or {anchor}: where to open once laid out
  const place = useRef(null)                    // the block at the top of the spread being read
  const landing = useRef(null)                  // the spread a goal's jump is landing on
  const tsRef = useRef(ts)
  tsRef.current = ts
  const pending = useRef(null)                  // a pointer down on a page edge
  const dragged = useRef(0)                     // when the last drag ended: its click is not a click
  const dragGone = useRef(null)                 // {id, t}: a drag let go by a lost capture, its pointerup still to come
  const queued = useRef([])                     // page keys pressed while a goal is landing, applied from where it lands
  const turnedAt = useRef(0)                    // when the last click turned a leaf
  const selAt = useRef(null)                    // {t, x, y}: the last click that turned, or that a double-click kept as selection
  const selecting = useRef(false)               // this double-click selects a word, it is not a turn
  const [barAt, setBarAt] = useState('label')   // the toolbar button in the tab order (a roving tabindex)
  const focusAfter = useRef(false)
  const focusIn = useRef(null)                  // what in the book last took focus, until focus leaves the book
  const sound = useRef(null)
  if (!sound.current) sound.current = createTurnSound({ enabled: soundOn })

  const scopeKey = JSON.stringify({ scope: scope || null, include: include || null, title: title || '', by: by || '' })

  // the blocks
  useEffect(() => {
    const ac = new AbortController()
    let alive = true
    setData(null); setErr('')
    const req = bookId ? api.bookBlocks(bookId, ac.signal) : api.liveBlocks({ scope, include, title, by }, ac.signal)
    Promise.all([req, fontsReady()]).then(([d]) => {
      if (!alive) return
      const blocks = cleanBlocks(Array.isArray(d && d.blocks) ? d.blocks : [])
      const chapters = chapterList(blocks)
      const at = Number.isInteger(chapter) && chapter > 0 ? chapters.find((c) => c.chapter === chapter) : null
      const saved = at ? null : readPlace(store, key)
      goal.current = at ? { block: at.blockIndex } : saved && saved.block < blocks.length ? { block: saved.block } : null
      place.current = null
      landing.current = null
      queued.current = []
      setTs(T.idle(0))
      setData({ blocks, chapters, title: (d && d.title) || title || 'Untitled', rebuilt: !!(d && d.rebuilt) })
    }).catch((e) => { if (alive && !(e && e.name === 'AbortError')) setErr(bookErrorText(e)) })
    return () => { alive = false; ac.abort() }
  }, [bookId, scopeKey, attempt]) // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => { if (data && windowId) wm.setTitle(windowId, data.title) }, [data, windowId])
  useEffect(() => () => sound.current && sound.current.close(), [])
  useEffect(() => {
    let mq
    try { mq = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)') } catch { mq = null }
    if (!mq || !mq.addEventListener) return
    const on = () => setReduced(mq.matches)
    mq.addEventListener('change', on)
    return () => mq.removeEventListener('change', on)
  }, [])
  useEffect(() => { try { rootRef.current && rootRef.current.focus({ preventScroll: true }) } catch { /* not focusable yet */ } }, [])

  // A turn (or a cross-fade) can remount the page that holds focus, which drops focus to the body,
  // and the page keys live on the book. After each commit, if the element focused in the book was
  // removed and nothing else took focus, focus goes to the new page on the same side (when a page
  // held it) or to the book itself.
  const onFocusIn = (e) => { focusIn.current = e.target }
  const onFocusOut = (e) => {
    const el = e.target, root = rootRef.current
    if (root && e.relatedTarget && root.contains(e.relatedTarget)) return
    // still in the document a moment later: the reader moved focus out of the book, so let it go
    // (a removed element is put back by the effect below)
    Promise.resolve().then(() => { if (focusIn.current === el && el.isConnected) focusIn.current = null })
  }
  useLayoutEffect(() => {
    const was = focusIn.current, root = rootRef.current
    if (!was || !root || was.isConnected || typeof document === 'undefined') return
    const ae = document.activeElement
    if (ae && ae !== document.body && ae !== document.documentElement) return // focus went somewhere on purpose
    focusIn.current = null
    const side = was.getAttribute && was.getAttribute('role') === 'region' ? (was.classList.contains('bb-left') ? 'left' : 'right') : ''
    const pg = side ? (root.querySelector(`.bb-static .bb-${side}[role=region]`) || root.querySelector('.bb-static [role=region]')) : null
    try { (pg || root).focus({ preventScroll: true }) } catch { /* fine */ }
  })

  // the stage's size: at once, then debounced
  useEffect(() => {
    const el = stageRef.current
    if (!el) return
    const read = () => setBox((b) => {
      const w = Math.round(el.clientWidth), h = Math.round(el.clientHeight)
      return b && b.w === w && b.h === h ? b : { w, h }
    })
    read()
    if (typeof ResizeObserver !== 'function') return
    let t
    const ro = new ResizeObserver(() => { clearTimeout(t); t = setTimeout(read, 180) })
    ro.observe(el)
    return () => { clearTimeout(t); ro.disconnect() }
  }, [data])

  // the desk's dock floats over the bottom of the screen; lift the bar clear of it when they meet
  const [dockClear, setDockClear] = useState(0)
  useEffect(() => {
    const el = rootRef.current
    if (!el || typeof document === 'undefined') return
    const read = () => {
      const dock = document.querySelector('.dock')
      if (!dock) { setDockClear(0); return }
      const r = el.getBoundingClientRect(), d = dock.getBoundingClientRect()
      const across = d.left < r.right && d.right > r.left
      setDockClear(across ? Math.max(0, Math.round(r.bottom - d.top + 8)) : 0)
    }
    read()
    window.addEventListener('resize', read)
    el.addEventListener('pointerenter', read)
    const ro = typeof ResizeObserver === 'function' ? new ResizeObserver(read) : null
    if (ro) ro.observe(el)
    return () => { window.removeEventListener('resize', read); el.removeEventListener('pointerenter', read); ro && ro.disconnect() }
  }, [])

  const wideNow = box ? isWide(box.w) : false
  const metrics = useMemo(() => (box ? pageMetrics(box.w, box.h, wideNow) : null), [box, wideNow])

  // pour the blocks into pages, in chunks; a new size lays out again from the same place
  useEffect(() => {
    if (!data || !metrics || !itemRef.current) return
    const item = itemRef.current
    const measure = (html) => { item.innerHTML = html; return item.getBoundingClientRect().height }
    // a new size: finish any turn in flight, then lay out again from the spread it lands on
    const prev = layRef.current
    if (!goal.current && prev.src === data && prev.pages.length) {
      const f = T.finish(tsRef.current)
      if (tsRef.current.turning) setTs(f)
      const sp = listRef.current[Math.min(f.at, listRef.current.length - 1)]
      const ks = sp ? [sp.left, sp.right].filter((k) => k != null) : []
      const pg = ks.length ? prev.pages[Math.min(...ks)] : null
      if (pg && pg.items.length) goal.current = { block: pg.items[0].blockIndex }
      else if (place.current !== null) goal.current = { block: place.current }
    }
    const blocks = data.blocks
    const m = metrics, wide = wideNow
    let next, all = [], stop = false, handle = null
    const run = (deadline) => {
      if (stop) return
      const t0 = performance.now()
      const shouldYield = deadline && deadline.timeRemaining ? () => deadline.timeRemaining() < 2 : () => performance.now() - t0 > 14
      let r
      try {
        r = layout(blocks, { measure, pageHeight: m.contentH, startAt: next, shouldYield })
      } catch (e) {
        setErr('The book could not be laid out.')
        console.error('[bound] layout', e)
        return
      }
      item.innerHTML = ''
      all = all.concat(r.pages)
      next = r.next
      setLay({ pages: all, done: r.done, i: r.done ? blocks.length : next.i, m, wide, src: data })
      if (!r.done) handle = schedule(run, !!goal.current)
    }
    run(null)
    return () => { stop = true; unschedule(handle) }
  }, [data, metrics, wideNow])

  const list = useMemo(() => spreads(lay.pages, { wide: lay.wide }), [lay])
  const count = list.length
  const listRef = useRef(list)
  listRef.current = list
  const layRef = useRef(lay)
  layRef.current = lay
  const at = Math.min(ts.at, Math.max(0, count - 1))

  // open at the goal once it is laid out
  useEffect(() => {
    const g = goal.current
    if (!g) { flushKeys(); return } // keys still waiting for pages not laid out yet
    if (!lay.pages.length) return
    const page = g.anchor ? anchorPage(lay.pages, g.anchor) : locate(lay.pages, g.block)
    if (page >= 0) {
      goal.current = null
      // save the place once the jump has landed, not the spread before it. The target is captured
      // here: the updater runs later, after the place effect may already have cleared `landing`.
      // Page keys pressed while it was landing are applied from where it lands, so none is undone
      const to = spreadOf(list, page), n = list.length
      const keys = queued.current
      const on = { list, pages: lay.pages, done: lay.done }
      const [fin, rest] = replay(T.jump(tsRef.current, to, n), keys, on)
      const applied = keys.slice(0, keys.length - rest.length)
      queued.current = rest
      landing.current = T.finish(fin).at
      if (applied.some((k) => k.startsWith('Arrow')) && landing.current !== to) sound.current.play()
      setTs((s) => replay(T.jump(s, to, n), applied, on)[0])
      setFade((f) => f + 1)
    } else if (lay.done) { goal.current = null; flushKeys() }
  }, [lay, list]) // eslint-disable-line react-hooks/exhaustive-deps

  // remember the place, and say where we are
  const settled = !ts.turning
  const spread = list[at]
  const chapterOf = (sp) => { const c = spreadChapter(sp, lay.pages); const f = data && data.chapters.find((x) => x.chapter === c); return f ? f.title : '' }
  const total = lay.done ? lay.pages.length : aboutTotal(lay.pages.length, lay.i, data ? data.blocks.length : 0)
  const label = spread ? pageLabel(spread, total, lay.done) : ''
  useEffect(() => {
    if (!settled || !spread || goal.current) return
    if (landing.current !== null) { if (ts.at !== landing.current) return; landing.current = null }
    const first = Math.min(...[spread.left, spread.right].filter((k) => k != null))
    const page = lay.pages[first]
    const block = page && page.items.length ? page.items[0].blockIndex : null
    if (block !== null) { place.current = block; writePlace(store, key, block) }
  // ts.at too: a landing on the spread the clamped `at` already shows changes nothing else, and
  // would otherwise never resolve (no place saved, nothing said until the reader moves)
  }, [settled, at, ts.at, spread, lay.pages, store, key])
  const spoken = spread ? announcement(pageLabel(spread, total, lay.done), chapterOf(spread)) : ''
  // Nothing is said while a goal is landing (a re-layout, a remembered place): the spread on screen
  // is the one before the jump, clamped to the pages laid out so far. The landed one is said when
  // it settles.
  const landingNow = () => !!goal.current || (landing.current !== null && ts.at !== landing.current)
  useEffect(() => { if (settled && spoken && !landingNow()) setLive(spoken) }, [settled, at, ts.at]) // eslint-disable-line react-hooks/exhaustive-deps
  // and once more when the count settles: "of about 40" becomes "of 42" (a turn in flight says it as it lands)
  useEffect(() => { if (lay.done && settled && spoken && !landingNow()) setLive(spoken) }, [lay.done]) // eslint-disable-line react-hooks/exhaustive-deps

  // keyboard turns move focus to the new page
  useEffect(() => {
    if (!settled || !focusAfter.current) return
    focusAfter.current = false
    const pg = stageRef.current && stageRef.current.querySelector('.bb-static [role=region]')
    try { if (pg) pg.focus({ preventScroll: true }) } catch { /* fine */ }
  }, [settled, at, fade])

  // the leaf in flight moves on animation frames
  const animating = !!(ts.turning && ts.turning.target !== null)
  useEffect(() => {
    if (!animating) return
    let raf, last = performance.now()
    const frame = (now) => {
      const dt = Math.min(64, Math.max(0, now - last))
      last = now
      setTs((s) => T.tick(s, dt))
      raf = requestAnimationFrame(frame)
    }
    raf = requestAnimationFrame(frame)
    return () => cancelAnimationFrame(raf)
  }, [animating])

  /* ---------------------------------------------------------------- actions */

  const canTurn = (dir) => { const f = T.finish(tsRef.current); const k = Math.min(f.at, listRef.current.length - 1) + dir; return k >= 0 && k < listRef.current.length }
  // A move the reader makes supersedes a goal's landing: their turn can be applied on top of the
  // pending jump, so it never settles on the landing spread, and the place would never be saved
  // again. Each of turn, jumpTo and the drag start clears it; the place effect then saves wherever
  // the book settles.
  const turn = useCallback((dir) => {
    landing.current = null
    if (!canTurn(dir)) { setTs((s) => T.finish(s)); return }
    sound.current.play()
    const n = listRef.current.length
    if (reduced) { setTs((s) => T.finish(T.step({ ...s, at: Math.min(s.at, n - 1) }, dir, n))); setFade((f) => f + 1) }
    else setTs((s) => T.step({ ...s, at: Math.min(s.at, n - 1) }, dir, n))
  }, [reduced])
  // the state after a page key from state s, over the spreads and pages in `on`
  const keyState = (s, k, on) => {
    const n = on.list.length
    if (!n) return s
    const cur = { ...s, at: Math.min(s.at, n - 1) }
    switch (k) {
      case 'ArrowRight': case 'ArrowLeft': {
        const next = T.step(cur, k === 'ArrowRight' ? 1 : -1, n)
        return reduced ? T.finish(next) : next
      }
      case 'Home': return T.jump(cur, 0, n)
      case 'End': return T.jump(cur, n - 1, n)
      case 'PageDown': case 'PageUp': {
        const to = chapterSpread(on.list, on.pages, T.finish(cur).at, k === 'PageDown' ? 1 : -1)
        return to < 0 ? cur : T.jump(cur, to, n)
      }
      default: return s
    }
  }
  // queued page keys applied in order: [state, the keys that wait]. While the layout runs, a key
  // going forward past what is laid out (→ at its end, End, PgDn with no next chapter yet) waits
  const replay = (s, keys, on) => {
    let st = s
    for (let i = 0; i < keys.length; i++) {
      const k = keys[i]
      const next = keyState(st, k, on)
      const forward = k === 'ArrowRight' || k === 'End' || k === 'PageDown'
      if (!on.done && forward && (k === 'End' || T.finish(next).at === T.finish(st).at)) return [st, keys.slice(i)]
      st = next
    }
    return [st, []]
  }
  // apply what can be applied of the queued keys, from where the book is now
  const flushKeys = () => {
    const keys = queued.current
    if (!keys.length) return
    const on = { list: listRef.current, pages: layRef.current.pages, done: layRef.current.done }
    const [fin, rest] = replay(tsRef.current, keys, on)
    if (rest.length === keys.length) return
    const applied = keys.slice(0, keys.length - rest.length)
    queued.current = rest
    landing.current = T.finish(fin).at
    if (applied.some((k) => k.startsWith('Arrow'))) sound.current.play()
    setTs((s) => replay(s, applied, on)[0])
    if (reduced || applied.some((k) => !k.startsWith('Arrow'))) setFade((f) => f + 1)
  }
  const jumpTo = (spreadIndex) => {
    if (spreadIndex < 0) return
    landing.current = null
    setTs((s) => T.jump(s, spreadIndex, listRef.current.length))
    setFade((f) => f + 1)
  }
  const goToPage = (page) => {
    const s = spreadOf(list, page)
    if (s < 0) return
    const cur = T.finish(ts).at
    if (s === cur) return
    if (Math.abs(s - cur) === 1) turn(s > cur ? 1 : -1)
    else jumpTo(s)
  }
  const goToBlock = (block) => {
    const page = locate(lay.pages, block)
    if (page >= 0) goToPage(page)
    else { goal.current = { block } }
    setPop('')
  }
  const follow = (href) => {
    const r = routeLink(href, lay.pages, lay.done)
    if (r.go === 'page') goToPage(r.page)
    else if (r.go === 'wait') goal.current = { anchor: r.id }
    else if (r.go === 'browser') setAsk(r.url)
  }
  const openInBrowser = () => {
    const u = ask
    setAsk('')
    if (u) openBrowser(browseAddressFor(u) || u)
  }
  const openNode = useCallback((nodeId) => {
    wm.open('reader', { props: { start: { node_id: nodeId } }, w: 720, h: 640 })
  }, [])
  const setPaperTo = (p) => { setPaper(p); writePref(store, PAPER_KEY, p) }
  const toggleSound = () => {
    const on = !soundOn
    setSoundOn(on)
    writePref(store, SOUND_KEY, on ? 'on' : 'off')
    if (on) sound.current.enable(); else sound.current.disable()
  }

  const closedNow = settled && spread && lay.pages.length && ((spread.right != null && lay.pages[spread.right] && lay.pages[spread.right].kind === 'cover' && spread.left == null)
    || (spread.left != null && lay.pages[spread.left] && lay.pages[spread.left].kind === 'back' && spread.right == null))

  const onKeyDown = (e) => {
    if (e.altKey || e.ctrlKey || e.metaKey) return
    if (e.key === 'Escape') { if (pop || ask) { setPop(''); setAsk(''); e.preventDefault() } return }
    if (e.target.closest && e.target.closest('input, textarea, select, .bb-pop, .bb-ask')) return
    // in the toolbar the arrows, Home and End move between its buttons; page keys work only on the book
    if (e.target.closest && e.target.closest('.bb-bar')) { barKey(e); return }
    // while the book is laid out again (or a goal is otherwise landing) a page key waits, and is
    // applied from where the reader lands, so the landing never undoes it
    if (PAGE_KEYS.has(e.key) && (goal.current || queued.current.length)) {
      queued.current = [...queued.current, e.key]
      e.preventDefault()
      focusAfter.current = true
      return
    }
    const n = listRef.current.length
    switch (e.key) {
      case 'ArrowRight': turn(1); break
      case 'ArrowLeft': turn(-1); break
      case 'Home': jumpTo(0); break
      case 'End': jumpTo(n - 1); break
      case 'PageDown': jumpTo(chapterSpread(listRef.current, lay.pages, T.finish(tsRef.current).at, 1)); break
      case 'PageUp': jumpTo(chapterSpread(listRef.current, lay.pages, T.finish(tsRef.current).at, -1)); break
      case 'Enter':
        if (!closedNow || (e.target !== rootRef.current && !e.target.closest('.bb-book'))) return
        turn(at === 0 ? 1 : -1)
        break
      default: return
    }
    e.preventDefault()
    focusAfter.current = true
  }

  // the toolbar's buttons, in order, as [name, disabled], for its roving tabindex
  const barItems = [['prev', !spread || at <= 0], ['label', !spread], ['next', !spread || at >= count - 1],
    ['thumbs', !spread], ['night', false], ['day', false], ['sound', false]]
  const rove = roveCurrent(barItems, barAt)
  const roving = (name) => ({ 'data-rove': name, tabIndex: name === rove ? 0 : -1, onFocus: () => setBarAt(name) })
  // A focused Next or Previous that becomes disabled (at the last spread or the cover) would drop
  // focus to the page body: it moves to the other one, else to the bar's stop, else to the book.
  const barOff = barItems.some(([n, off]) => n === barAt && off)
  useEffect(() => {
    if (!barOff || typeof document === 'undefined' || !rootRef.current) return
    const was = rootRef.current.querySelector(`.bb-bar [data-rove="${barAt}"]`)
    const ae = document.activeElement
    if (ae && ae !== document.body && ae !== was) return // focus is somewhere else on purpose
    const other = barAt === 'next' ? 'prev' : barAt === 'prev' ? 'next' : null
    const to = other && barItems.some(([n, off]) => n === other && !off) ? other : rove
    const el = to && rootRef.current.querySelector(`.bb-bar [data-rove="${to}"]`)
    try { if (el) { setBarAt(to); el.focus() } else rootRef.current.focus({ preventScroll: true }) } catch { /* fine */ }
  }, [barOff, barAt]) // eslint-disable-line react-hooks/exhaustive-deps
  const barKey = (e) => {
    const from = e.target.closest('[data-rove]')
    const to = roveNext(barItems, from ? from.getAttribute('data-rove') : rove, e.key)
    if (!to) return
    e.preventDefault()
    setBarAt(to)
    const el = rootRef.current && rootRef.current.querySelector(`.bb-bar [data-rove="${to}"]`)
    try { if (el) el.focus() } catch { /* fine */ }
  }

  // A press soon after a click that turned a leaf, near where it was: the second click of a
  // double-click. Each click kept as selection opens the window again, so a triple-click (or more)
  // stays selection too.
  const quickSecond = (e) => {
    const c = selAt.current
    return !!(c && Date.now() - c.t < SELECT_MS && Math.abs(e.clientX - c.x) <= 8 && Math.abs(e.clientY - c.y) <= 8)
  }
  const onPointerDown = (e) => {
    dragged.current = 0
    dragGone.current = null
    if (e.button !== 0) return
    // The second click of a double-click is decided here, from the pointer: in the drag band the
    // pointerdown below is prevented, which suppresses the mousedown, so onMouseDown never sees it.
    // It is left alone, so the browser selects the word, and its click does not turn.
    selecting.current = quickSecond(e)
    if (selecting.current || reduced || closedNow || !bookRef.current) return
    if (e.target.closest('a, button, input, .bb-pop')) return
    const r = bookRef.current.getBoundingClientRect()
    const pw = lay.wide ? r.width / 2 : r.width
    const band = pw * 0.16
    const dir = e.clientX > r.right - band ? 1 : e.clientX < r.left + band ? -1 : 0
    if (!dir) return
    // preventing the press suppresses the mousedown that would focus the page: focus the book
    // here instead, so its page keys work after the turn (the narrow spine is in this band)
    const root = rootRef.current
    if (root && !root.contains(document.activeElement)) { try { root.focus({ preventScroll: true }) } catch { /* fine */ } }
    e.preventDefault()
    pending.current = { dir, x0: e.clientX, spine: lay.wide ? r.left + pw : r.left, pw, started: false, narrowBack: !lay.wide && dir < 0, id: e.pointerId }
    try { e.currentTarget.setPointerCapture(e.pointerId) } catch { /* fine */ }
  }
  const onPointerMove = (e) => {
    const d = pending.current
    if (!d) return
    if (!d.started) {
      if (Math.abs(e.clientX - d.x0) < 5) return
      d.started = true
      landing.current = null
      const n = listRef.current.length
      setTs((s) => T.start({ ...s, at: Math.min(s.at, n - 1) }, d.dir, n))
    }
    const p = d.narrowBack ? (e.clientX - d.x0) / (d.pw * 0.9) : T.dragProgress(e.clientX, d.spine, d.pw, d.dir)
    setTs((s) => T.drag(s, p))
  }
  // The leaf is let go on a pointerup, or when the capture is lost, the pointer cancelled or the
  // window blurred. Only a real drag's own pointerup suppresses the click after it: a drag let go
  // any other way leaves the next click alone, unless that drag's pointerup still comes.
  const letGo = (real, pointerId) => {
    const d = pending.current
    if (!d) {
      // a blur or lost capture after the drag was let go changes nothing: its pointerup may still come
      if (!real) return
      const g = dragGone.current
      dragGone.current = null // a real pointerup ends it either way
      if (g && pointerId === g.id && Date.now() - g.t < 1000) dragged.current = Date.now()
      return
    }
    pending.current = null
    if (!d.started) return
    if (real) dragged.current = Date.now()
    else dragGone.current = d.id == null ? null : { id: d.id, t: Date.now() }
    const t = tsRef.current.turning
    if (t && t.target === null && t.progress > 0.5) sound.current.play()
    setTs((s) => T.release(s))
  }
  const onPointerUp = (e) => letGo(true, e.pointerId)
  const onPointerLost = (e) => letGo(false, e.pointerId)
  const zoneOf = (e) => (bookRef.current ? clickZone(e.clientX, bookRef.current.getBoundingClientRect(), lay.wide) : 0)
  const onClick = (e) => {
    if (dragged.current && Date.now() - dragged.current < 400) { dragged.current = 0; return }
    dragged.current = 0
    if (linkEvent(e, follow)) return
    if (e.target.closest('button, input, .bb-pop, .bb-ask') || !bookRef.current) return
    const prev = selAt.current
    turnedAt.current = 0
    selAt.current = null
    const here = () => ({ t: Date.now(), x: e.clientX, y: e.clientY })
    const turned = () => { turnedAt.current = Date.now(); selAt.current = here() }
    // a double- or triple-click that selects is not a turn (see onPointerDown and onMouseDown), nor
    // is any click after the first within SELECT_MS of the last turn or selection click
    const select = e.detail > 1 && (selecting.current || (prev && Date.now() - prev.t < SELECT_MS))
    selecting.current = false
    if (select) { selAt.current = here(); return }
    if (closedNow) { turned(); turn(at === 0 ? 1 : -1); return }
    // a selection made by dragging over the text is not a turn; a slower second click is
    if (e.detail < 2) { try { if (String(window.getSelection && window.getSelection()).length) return } catch { /* none */ } }
    const dir = zoneOf(e)
    if (dir) { turned(); turn(dir) } // a turn in flight finishes first, so each click is one leaf
  }
  // A second click in the outer third within SELECT_MS of a click that turned a leaf (or after a
  // click that did not turn) selects a word, as a double-click does anywhere. A slower one, still
  // counted as a double-click by the system, turns again: its word selection is prevented.
  const onMouseDown = (e) => {
    if (e.button === 1 && e.target.closest && e.target.closest('a')) { e.preventDefault(); return }
    if (e.button !== 0) return
    if (e.detail < 2 || selecting.current) return
    if (e.target.closest('a, button, input, .bb-pop, .bb-ask') || !zoneOf(e)) return
    if (turnedAt.current && Date.now() - turnedAt.current >= SELECT_MS) e.preventDefault()
    else selecting.current = true
  }
  const onAuxClick = (e) => { linkEvent(e, follow) }
  // the window losing focus mid-drag lets the leaf go the normal way (release), never left hanging
  const releaseRef = useRef(null)
  releaseRef.current = () => letGo(false, null)
  useEffect(() => {
    const blur = () => releaseRef.current && releaseRef.current()
    window.addEventListener('blur', blur)
    return () => window.removeEventListener('blur', blur)
  }, [])
  const onDragStart = (e) => { if (e.target.closest && e.target.closest('a')) e.preventDefault() }

  /* ---------------------------------------------------------------- drawing */

  const m = lay.m
  const kindOf = useCallback((k) => (data && data.blocks[k] ? data.blocks[k].kind : ''), [data])
  const nodeOf = useCallback((k) => (data && data.blocks[k] ? data.blocks[k].node_id || '' : ''), [data])
  const chapterTitle = (k) => {
    const p = lay.pages[k]
    if (!p || !data) return ''
    const f = data.chapters.find((c) => c.chapter === p.chapter)
    return f ? f.title : ''
  }
  const page = (k, side, hidden = false) => (
    <PageView key={`${side}-${k}-${hidden ? 'h' : 's'}`} page={k == null ? null : lay.pages[k]} index={k ?? 0} side={side}
              bookTitle={data ? data.title : ''} chapterTitle={k == null ? '' : chapterTitle(k)} kindOf={kindOf} nodeOf={nodeOf}
              hidden={hidden} onOpenNode={openNode} />
  )
  const reverse = <div className="bb-page bb-reverse" aria-hidden="true" />

  let book = null
  if (m && spread) {
    const t = ts.turning && list[at + ts.turning.dir] ? ts.turning : null
    const A = sides(spread, lay.wide)
    let left = A.left, right = A.right, leaf = null, shift = shiftOf(spread, lay.wide, m.pw), closed = closedNow ? 1 : 0
    if (t) {
      const B = sides(list[at + t.dir], lay.wide)
      const p = T.shown(t)
      const isClosed = (sp) => !!sp && ((sp.left == null && sp.right != null && lay.pages[sp.right]?.kind === 'cover')
        || (sp.right == null && sp.left != null && lay.pages[sp.left]?.kind === 'back'))
      const cA = isClosed(spread) ? 1 : 0, cB = isClosed(list[at + t.dir]) ? 1 : 0
      closed = cA * (1 - p) + cB * p
      shift = shiftOf(spread, lay.wide, m.pw) * (1 - p) + shiftOf(list[at + t.dir], lay.wide, m.pw) * p
      const fwd = t.dir > 0
      left = fwd ? A.left : B.left
      right = fwd ? B.right : A.right
      const frontK = fwd ? A.right : B.right
      const backK = fwd ? B.left : A.left
      leaf = (
        <>
          <Cast turning={t} pw={m.pw} wide={lay.wide} />
          <Leaf turning={t} pw={m.pw} wide={lay.wide}
                front={frontK == null ? reverse : page(frontK, 'right', true)}
                back={!lay.wide || backK == null ? reverse : page(backK, 'left', true)} />
        </>
      )
    }
    // lying flat and centred the book has no transform at all, so its text is drawn crisply
    const transform = !t && !closed && Math.abs(shift) < 0.05 ? 'none'
      : `translateX(${shift.toFixed(1)}px) rotateX(${(CLOSED_TILT.x * closed).toFixed(2)}deg) `
        + `rotateZ(${(CLOSED_TILT.z * closed).toFixed(2)}deg) scale(${(1 - CLOSED_TILT.scale * closed).toFixed(3)})`
    book = (
      <div className={'bb-book' + (lay.wide ? ' bb-wide' : ' bb-narrow') + (closedNow ? ' bb-closed' : '')} ref={bookRef}
           style={{ ...cssVars(m), width: lay.wide ? 2 * m.pw : m.pw, height: m.ph, transform }}
           title={closedNow ? (at === 0 ? 'Open the book' : 'Open the book from the back') : undefined}>
        <div className="bb-desk-shadow" aria-hidden="true" />
        <div className={'bb-static' + (fade ? ' bb-fade' : '')} key={`static-${fade}`}>
          {lay.wide ? (left != null ? page(left, 'left') : <div className="bb-page bb-left bb-none" aria-hidden="true" />) : null}
          {right != null ? page(right, 'right')
            : lay.wide && (t || spread.blankRight) ? <div className="bb-page bb-right bb-blank" aria-hidden="true" />
              : <div className="bb-page bb-right bb-none" aria-hidden="true" />}
        </div>
        {leaf}
      </div>
    )
  }

  const chapters = data ? data.chapters : []
  const curChapter = spread ? spreadChapter(spread, lay.pages) : 0
  const thumbs = pop === 'thumbs' ? (
    <div className="bb-pop bb-thumbs" role="group" aria-label="Thumbnails">
      {list.map((sp, s) => {
        const ks = [sp.left, sp.right].filter((k) => k != null)
        return (
          <button key={s} type="button" className={'bb-thumb' + (s === at ? ' bb-thumb-on' : '')} aria-current={s === at || undefined}
                  aria-label={pageLabel(sp, total, lay.done)}
                  onClick={() => { jumpTo(s); setPop('') }}>
            <span className="bb-thumb-pages">
              {ks.map((k) => {
                const pic = pagePicture(lay.pages[k])
                return (
                  <span key={k} className={'bb-thumb-page' + (lay.pages[k].kind ? ' bb-thumb-' + lay.pages[k].kind : '')}>
                    {pic ? <img src={pic} alt="" loading="lazy" /> : <span className="bb-thumb-lines" />}
                  </span>
                )
              })}
            </span>
            <span className="bb-thumb-label">{ks.map((k) => k + 1).join('–')}</span>
          </button>
        )
      })}
    </div>
  ) : null

  const goto = pop === 'goto' ? (
    <div className="bb-pop bb-goto" role="dialog" aria-label="Go to page or chapter">
      <form className="bb-goto-page" onSubmit={(e) => {
        e.preventDefault()
        const n = parseInt(new FormData(e.currentTarget).get('page'), 10)
        if (n >= 1 && n <= lay.pages.length) { goToPage(n - 1); setPop('') }
      }}>
        <label>Page <input name="page" type="number" min="1" max={lay.pages.length || 1} defaultValue={spread ? (spread.left ?? spread.right) + 1 : 1} /></label>
        <Button size="sm" type="submit">Go</Button>
      </form>
      <ol className="bb-goto-list" aria-label="Chapters">
        <li><button type="button" onClick={() => { jumpTo(0); setPop('') }}>Cover</button></li>
        {chapters.map((c) => (
          <li key={c.blockIndex}>
            <button type="button" aria-current={c.chapter === curChapter || undefined} onClick={() => goToBlock(c.blockIndex)}>
              <span className="bb-goto-n">{c.chapter}</span> {c.title || 'Untitled'}
            </button>
          </li>
        ))}
        <li><button type="button" onClick={() => { jumpTo(list.length - 1); setPop('') }}>Back cover</button></li>
      </ol>
    </div>
  ) : null

  useEffect(() => {
    if (pop !== 'thumbs') return
    const el = rootRef.current && rootRef.current.querySelector('.bb-thumb-on')
    try { if (el && el.scrollIntoView) el.scrollIntoView({ block: 'nearest', inline: 'center' }) } catch { /* fine */ }
  }, [pop])

  const busy = !err && (!data || !book)
  return (
    <div className={`bb bb-${paper}${reduced ? ' bb-reduced' : ''}`} ref={rootRef} tabIndex={0} onKeyDown={onKeyDown} onFocus={onFocusIn} onBlur={onFocusOut} role="group"
         style={dockClear ? { paddingBottom: dockClear } : undefined}
         aria-label={data ? `${data.title}, a book` : 'A book'}>
      {data && data.rebuilt ? <div className="bb-note" role="status">{REBUILT_NOTE}</div> : null}
      <div className="bb-stage" ref={stageRef} onClick={onClick} onPointerDown={onPointerDown} onPointerMove={onPointerMove}
           onPointerUp={onPointerUp} onPointerCancel={onPointerLost} onLostPointerCapture={onPointerLost}
           onMouseDown={onMouseDown} onAuxClick={onAuxClick} onDragStart={onDragStart} style={{ '--bar-h': BAR_H + 'px' }}>
        {err ? (
          <div className="bb-msg" role="alert">
            <div>{err}</div>
            <Button size="sm" onClick={() => setAttempt((n) => n + 1)}>Try again</Button>
          </div>
        ) : null}
        {busy ? <div className="bb-msg" role="status">{data ? 'Laying out the pages…' : 'Opening the book…'}</div> : null}
        {data && data.blocks.length === 0 ? <div className="bb-msg">This book has no pages.</div> : null}
        {book}
        {ask ? (
          <div className="bb-ask" role="alertdialog" aria-label={ASK_BROWSER}>
            <span className="bb-ask-q">{ASK_BROWSER}</span>
            <span className="bb-ask-url">{ask}</span>
            <span className="bb-ask-btns">
              <Button size="sm" variant="primary" autoFocus onClick={openInBrowser}>Open</Button>
              <Button size="sm" variant="ghost" onClick={() => setAsk('')}>Cancel</Button>
            </span>
          </div>
        ) : null}
      </div>
      {thumbs}
      {goto}
      <div className="bb-bar" role="toolbar" aria-label="Book">
        <Button variant="ghost" size="sm" className="bb-arrow" aria-label="Previous page" {...roving('prev')} disabled={!spread || at <= 0} onClick={() => { turn(-1) }}>‹</Button>
        <button type="button" className="bb-label" {...roving('label')} aria-haspopup="dialog" aria-expanded={pop === 'goto'} disabled={!spread}
                title="Go to a page or chapter" onClick={() => setPop((p) => (p === 'goto' ? '' : 'goto'))}>{label || '…'}</button>
        <Button variant="ghost" size="sm" className="bb-arrow" aria-label="Next page" {...roving('next')} disabled={!spread || at >= count - 1} onClick={() => { turn(1) }}>›</Button>
        <span className="bb-sep" />
        <Button variant="ghost" size="sm" {...roving('thumbs')} aria-label="Thumbnails" title="Thumbnails" aria-pressed={pop === 'thumbs'} disabled={!spread}
                onClick={() => setPop((p) => (p === 'thumbs' ? '' : 'thumbs'))}><Eye /></Button>
        <span className="bb-paper" role="group" aria-label="Paper">
          <Button variant="ghost" size="sm" {...roving('night')} aria-pressed={paper === 'night'} onClick={() => setPaperTo('night')}>Night</Button>
          <Button variant="ghost" size="sm" {...roving('day')} aria-pressed={paper === 'day'} onClick={() => setPaperTo('day')}>Day</Button>
        </span>
        <Button variant="ghost" size="sm" {...roving('sound')} aria-pressed={soundOn} title="A soft paper sound when a page turns" onClick={toggleSound}>Sound</Button>
      </div>
      <div className="bb-sr" aria-live="polite">{live}</div>
      {metrics ? (
        <div className={`bb-measure bb-${paper}`} aria-hidden="true" style={cssVars(metrics)}>
          <section className="bb-page"><div className="bb-content"><div className="bb-item" ref={itemRef} /></div></section>
        </div>
      ) : null}
    </div>
  )
}
