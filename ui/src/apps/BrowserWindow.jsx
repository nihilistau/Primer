/* BrowserWindow — a reader's browser: web pages and offline Wikipedia articles as clean
 * Markdown with links and pictures. Address bar, back/forward/reload, a selection menu
 * (Explore, Ask), and a side panel with Ask (about this page) and Click through (Primer
 * follows links toward a goal, the window going where it goes). Make a page opens a Primer
 * page grounded in this page alone. Props: { windowId, start? } (an address to open first).
 *
 * LIVE (Phase 14): the same window can show a page live, as a picture of a headless Chrome that
 * Primer drives. Clicks, scrolling and typing (Esc too) go to it while the picture has focus;
 * Shift+Esc leaves it. The address bar follows the page (nav events), the page follows the
 * window's size, and Ask and Make a page read the live page's own text. Click through drives the
 * live page, and asks before it presses, types or chooses. Closing the window, minimising it or
 * going back to Reader closes the live tab; a minimised window opens live again at the same
 * address when it is shown. */
import { useCallback, useEffect, useId, useRef, useState } from 'react'
import * as api from '../api.js'
import * as wm from '../windowManager.js'
import { Button, Chip, Input, State } from '../kit/parts.jsx'
import { Markdown } from '../reader/markdown.jsx'
import { safeHref } from '../reader/citations.js'
import { OPEN_BROWSER_EVENT, openBrowser } from '../openBrowser.js'
import { addressFromInput, clampX, createNav, pageConcept, stepLink, stepTarget } from './browserNav.js'
import { askPrefill, cleanSelection, selectionMenuItems } from './selection.js'
import {
  HOVER_MS, askParts, b64Bytes, capPaste, createClickCounter, createInputQueue, framePoint, keyAction, liveSize,
  liveFallback, liveStepAction, liveable, modifiers, mouseButton, newWindowId, offerLive, pageTargets, wheelDelta,
} from './live.js'
import './apps.css'

const whereName = (w) => (w === 'reference' ? 'offline encyclopedia' : 'web')

/* the live address a minimised window had, so showing it again opens it live again; an entry
 * goes when its window closes */
const resume = new Map()
wm.subscribe(() => {
  if (!resume.size) return
  const ids = new Set(wm.getWindows().map(w => w.id))
  for (const id of [...resume.keys()]) if (!ids.has(id)) resume.delete(id)
})
const RESIZE_MS = 300  // a resize is sent once the window has stopped changing size this long

const GONE = {
  closed: 'The live page has closed.',
  idle: 'The live page closed after a while with nothing happening.',
  Forgotten: 'Primer’s browser forgot everything, so the live page closed.',
}
const goneText = (r) => GONE[r] || (r ? (/[.!?]$/.test(r) ? r : r + '.') : GONE.closed)

/* Answer the model's ask: Allow once (allow true) or Stop (false). */
export const answerLiveAsk = (liveId, ask, allow) => api.liveAnswer(liveId, ask.id, allow)

/* The model's ask, with its label and value in bold (as text: React escapes them). */
export function AskPrompt({ ask, onAnswer }) {
  return (
    <div className="bw-ask" role="group" aria-label="The model asks first">
      <p className="bw-ask-t">
        {askParts(ask).map((p, i) => (typeof p === 'string' ? <span key={i}>{p}</span> : <b key={i}>{p.b}</b>))}
      </p>
      <div className="bw-ask-b">
        <Button size="sm" variant="primary" onClick={() => onAnswer(true)}>Allow once</Button>
        <Button size="sm" onClick={() => onAnswer(false)}>Stop</Button>
      </div>
    </div>
  )
}

/* A live Click through's steps: what it saw, what it chose, why, and a step Primer refused. */
export function LiveSteps({ steps }) {
  if (!steps || !steps.length) return null
  return (
    <ol className="bw-steps">
      {steps.map((s, i) => (
        <li key={s.n ?? i}>
          <div className="bw-step-t">{s.saw || s.url}</div>
          {liveStepAction(s) ? <div className="bw-step-a">{liveStepAction(s)}</div> : null}
          {s.why ? <div className="bw-step-w">{s.why}</div> : null}
          {s.refused ? <div className="bw-step-r">Not done: {s.refused}</div> : null}
        </li>
      ))}
    </ol>
  )
}

export default function BrowserWindow({ windowId, start }) {
  const nav = useRef(createNav())
  const [, tick] = useState(0)
  const [addr, setAddr] = useState(start || '')
  const [view, setView] = useState(null)         // a page {u, title, markdown, where, origin_href} or {search, where, results}
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [failed, setFailed] = useState(null)   // the address that failed, when it may be opened live
  const [st, setSt] = useState(null)             // /v1/status
  const loadId = useRef(0)
  const loadAbort = useRef(null)
  const bodyRef = useRef(null)
  const askRef = useRef(null)
  const [menu, setMenu] = useState(null)         // {text, x, y} while the selection menu shows

  /* side panel */
  const [tab, setTab] = useState('ask')
  const [question, setQuestion] = useState('')
  const [ask, setAsk] = useState({ running: false, text: '', error: '' })
  const askAbort = useRef(null)
  const [goal, setGoal] = useState('')
  const [go, setGo] = useState({ running: false, steps: [], answer: '', note: '', error: '', waiting: false, ask: null })
  const goAbort = useRef(null)

  /* live: {id, u, title, opening, error, gone, note, msg, fw, fh, framed} while the window is live */
  const [live, setLive] = useState(null)
  const liveRef = useRef(null)                   // {id, u, ac, gone, queue} of the live tab now shown
  const picRef = useRef(null)
  const imgRef = useRef(null)
  const blobRef = useRef('')
  const dims = useRef({ w: 0, h: 0 })
  const rafRef = useRef(0)
  const down = useRef(null)                      // {button, clicks} while a button is down
  const lastHover = useRef(0)
  const hoverTimer = useRef(0)                   // the trailing hover move, sent when the pointer rests
  const clicks = useRef(createClickCounter())    // double and triple clicks, counted here
  const barRef = useRef(null)
  const moreRef = useRef(null)
  const hintId = useId()

  /* the window's menu, and Forget everything's confirmation */
  const [wmenu, setWmenu] = useState(false)
  const [forget, setForget] = useState({ open: false, busy: false, error: '' })
  const [forgotten, setForgotten] = useState('')
  const menuItemRef = useRef(null)

  useEffect(() => { api.status().then(setSt).catch(() => setSt(null)) }, [])

  const stopAsk = () => { if (askAbort.current) askAbort.current.abort(); askAbort.current = null }
  const stopGo = () => { if (goAbort.current) goAbort.current.abort(); goAbort.current = null }

  /* Close the live tab on show (if any): its frames stop, a click-through ends (the server sees
   * the client go: Stop), and the server is told. */
  const stopLive = () => {
    const m = liveRef.current
    if (!m) return
    liveRef.current = null
    m.ac.abort()
    m.queue.clear()
    if (goAbort.current) { stopGo(); setGo(g => ({ ...g, running: false, waiting: false, ask: null })) }
    if (rafRef.current) { cancelAnimationFrame(rafRef.current); rafRef.current = 0 }
    clearTimeout(hoverTimer.current); hoverTimer.current = 0
    clicks.current.reset()
    down.current = null
    if (blobRef.current) { URL.revokeObjectURL(blobRef.current); blobRef.current = '' }
    dims.current = { w: 0, h: 0 }
    if (!m.gone) api.liveClose(m.id).catch(() => {})
  }

  useEffect(() => {
    // a tab going away closes its live page too (keepalive)
    const bye = () => { const m = liveRef.current; if (m && !m.gone) api.liveClose(m.id).catch(() => {}) }
    window.addEventListener('pagehide', bye)
    return () => {
      window.removeEventListener('pagehide', bye)
      const m = liveRef.current
      // still a window after unmounting: it was minimised, so it opens live again when shown
      if (m && !m.gone && wm.getWindows().some(w => w.id === windowId)) resume.set(windowId, { u: m.u, title: m.title })
      stopLive(); stopAsk(); stopGo(); loadId.current++; if (loadAbort.current) loadAbort.current.abort()
    }
  }, [])

  /* Moving this window anywhere ends an answer in flight; a click-through ends too unless
   * it is the click-through itself moving (byAgent). */
  const settle = (byAgent) => {
    if (!byAgent && goAbort.current) { stopGo(); setGo(g => ({ ...g, running: false, waiting: false, ask: null, note: 'Stopped.' })) }
    stopAsk(); setAsk({ running: false, text: '', error: '' })
    setMenu(null)
  }

  /* Open an address in this window. mode: 'push' (a new history step), 'none' (back/forward),
   * 'replace' (reload). Going to the reader closes a live page. */
  const begin = (byAgent) => {
    settle(byAgent)
    if (liveRef.current) { stopLive(); setLive(null) }
    if (loadAbort.current) loadAbort.current.abort()
    const ac = new AbortController(); loadAbort.current = ac
    setLoading(true); setError(''); setFailed(null)
    return { id: ++loadId.current, signal: ac.signal }
  }
  // a failed open leaves the address bar showing the page that is still on screen
  const restoreAddr = () => { const c = nav.current.current(); if (c) setAddr(addrOf(c)) }
  /* mode 'back'/'forward' move the history only once the page has opened */
  const commit = (mode, entry) => {
    if (mode === 'push') nav.current.push(entry)
    else { if (mode === 'back') nav.current.move(-1); else if (mode === 'forward') nav.current.move(1); nav.current.replace(entry) }
  }
  const load = useCallback(async (u, mode = 'push', byAgent = false) => {
    const { id, signal } = begin(byAgent)
    setAddr(u)
    try {
      const p = await api.browse(u, signal)
      if (id !== loadId.current) return
      const entry = { u: p.final_url || u, title: p.title }
      commit(mode, entry)
      setView({ ...p, u: entry.u })
      setAddr(entry.u)
      if (bodyRef.current) bodyRef.current.scrollTop = 0
    } catch (e) {
      if (id !== loadId.current) return
      setError(e.message || 'That page could not be opened.')
      setFailed(liveFallback(u, e))
      restoreAddr()
    }
    if (id === loadId.current) setLoading(false)
    tick(x => x + 1)
  }, [])

  const search = useCallback(async (q, where, mode = 'push') => {
    const { id, signal } = begin(false)
    try {
      const r = await api.browseSearch(q, where, signal)
      if (id !== loadId.current) return
      commit(mode, { u: `search:${where}:${q}`, title: `Search: ${q}`, search: { q, where } })
      setView({ search: q, where, results: r.results || [] })
    } catch (e) {
      if (id !== loadId.current) return
      setError(e.message || 'The search failed.')
      restoreAddr()
    }
    if (id === loadId.current) setLoading(false)
    tick(x => x + 1)
  }, [])

  /* ---- live ---------------------------------------------------------------------------- */

  const setLiveU = (u, title) => {
    if (liveRef.current) { liveRef.current.u = u; if (title != null) liveRef.current.title = title }
    setLive(l => (l ? { ...l, u, title: title ?? l.title } : l))
    setAddr(u)
  }

  const draw = (m, d) => {
    const img = imgRef.current
    if (!img || !d || typeof d.data !== 'string') return
    let url
    try { url = URL.createObjectURL(new Blob([b64Bytes(d.data)], { type: 'image/jpeg' })) } catch { return }
    const old = blobRef.current
    blobRef.current = url
    img.src = url
    if (old) URL.revokeObjectURL(old)
    const w = Number(d.w) || 0, h = Number(d.h) || 0
    if (w !== dims.current.w || h !== dims.current.h || !m.framed) {
      dims.current = { w, h }; m.framed = true
      setLive(l => (l ? { ...l, fw: w, fh: h, framed: true } : l))
    }
  }

  const openLive = async (u, title = '') => {
    if (!liveable(u)) { setError('Only web addresses can be opened live.'); return }
    settle(false)
    stopLive()
    loadId.current++; if (loadAbort.current) loadAbort.current.abort()
    setLoading(false); setError(''); setFailed(null); setForgotten('')
    const id = newWindowId()
    const m = { id, u, title, ac: new AbortController(), gone: false, framed: false }
    m.queue = createInputQueue((ev) => api.liveInput(id, ev), (err) => {
      if (liveRef.current === m && err && err.status !== 404) setLive(l => (l ? { ...l, msg: err.message || 'The page didn’t take that.' } : l))
    })
    liveRef.current = m
    setLive({ id, u, title, opening: true, error: '', gone: '', note: '', msg: '', fw: 0, fh: 0, framed: false })
    setAddr(u)
    const box = bodyRef.current
    const width = liveSize(box ? box.clientWidth : 800), height = liveSize(box ? box.clientHeight : 600)
    m.size = { w: width, h: height }
    try {
      await api.liveOpen(id, u, width, height)
    } catch (e) {
      if (liveRef.current !== m) return
      m.gone = true
      setLive(l => (l ? { ...l, opening: false, error: e.message || 'That page could not be opened live.' } : l))
      return
    }
    if (liveRef.current !== m) { api.liveClose(id).catch(() => {}); return }
    setLive(l => (l ? { ...l, opening: false } : l))
    let reason = '', told = false
    try {
      await api.liveFrames(id, ({ event, data }) => {
        if (liveRef.current !== m) return
        if (event === 'frame') draw(m, data)
        else if (event === 'note') setLive(l => (l ? { ...l, note: (data && data.text) || '' } : l))
        else if (event === 'nav' && data && liveable(data.url)) setLiveU(data.url, typeof data.title === 'string' ? data.title : '')
        else if (event === 'gone') { told = true; reason = (data && data.reason) || 'closed' }
      }, m.ac.signal)
    } catch (e) {
      if (e.name === 'AbortError' || liveRef.current !== m) return
      reason = e.message || ''
    }
    if (liveRef.current !== m) return
    // the stream broke without saying the window ended: it may still be open, so close it
    if (!told) api.liveClose(id).catch(() => {})
    m.gone = true
    m.queue.clear()
    if (goAbort.current) { stopGo(); setGo(g => ({ ...g, running: false, waiting: false, ask: null })) }
    setLive(l => (l ? { ...l, gone: goneText(reason || 'closed') } : l))
  }

  /* back to the reader view of the same address */
  const toReader = () => {
    const u = (liveRef.current && liveRef.current.u) || (live && live.u) || ''
    stopLive(); setLive(null)
    if (u) load(u)
  }

  const liveNavTo = (action, u) => {
    const m = liveRef.current
    if (!m || m.gone) return
    setLive(l => (l ? { ...l, msg: '' } : l))
    if (action === 'url') setLiveU(u)
    api.liveNav(m.id, action, u).catch((e) => {
      if (liveRef.current === m) setLive(l => (l ? { ...l, msg: e.message || 'That didn’t work.' } : l))
    })
  }

  /* input: queued, and sent once per animation frame */
  const schedule = () => {
    if (rafRef.current) return
    rafRef.current = requestAnimationFrame(() => {
      rafRef.current = 0
      const m = liveRef.current
      if (!m) return
      m.queue.flush().then(() => { if (liveRef.current === m && m.queue.pending()) schedule() })
    })
  }
  const sendIn = (ev) => {
    const m = liveRef.current
    if (!m || m.gone || !m.framed) return
    m.queue.push(ev)
    schedule()
  }
  const pt = (e, clamp) => {
    const img = imgRef.current
    if (!img) return null
    return framePoint(e.clientX, e.clientY, img.getBoundingClientRect(), dims.current.w, dims.current.h, clamp)
  }
  const onPicDown = (e) => {
    if (picRef.current) picRef.current.focus()
    e.preventDefault()
    if (e.button > 2 || down.current) return
    const p = pt(e, false)
    if (!p) return
    try { e.currentTarget.setPointerCapture(e.pointerId) } catch { /* fine */ }
    const button = mouseButton(e.button)
    const n = clicks.current.press(e.clientX, e.clientY, button, performance.now())
    down.current = { button, clicks: n }
    sendIn({ kind: 'mouse', type: 'down', ...p, button, clicks: n, modifiers: modifiers(e) })
  }
  const onPicMove = (e) => {
    const d = down.current
    const p = pt(e, !!d)
    if (!p) return
    const ev = { kind: 'mouse', type: 'move', ...p, button: d ? d.button : 'none', clicks: 0, modifiers: modifiers(e) }
    clearTimeout(hoverTimer.current); hoverTimer.current = 0
    if (!d) {
      const now = performance.now(), wait = HOVER_MS - (now - lastHover.current)
      if (wait > 0) {  // too soon: keep it, and send it if the pointer rests here
        hoverTimer.current = setTimeout(() => { hoverTimer.current = 0; lastHover.current = performance.now(); sendIn(ev) }, wait)
        return
      }
      lastHover.current = now
    }
    sendIn(ev)
  }
  const onPicUp = (e) => {
    const d = down.current
    if (!d) return
    down.current = null
    const p = pt(e, true)
    if (p) sendIn({ kind: 'mouse', type: 'up', ...p, button: d.button, clicks: d.clicks, modifiers: modifiers(e) })
  }
  // Shift+Esc (or the Leave button): focus goes back to the bar's Reader button
  const leavePicture = () => {
    const b = barRef.current && barRef.current.querySelector('[data-focus="reader"]')
    if (b) b.focus(); else if (barRef.current) { const i = barRef.current.querySelector('input'); if (i) i.focus() }
  }
  const onPicKey = (type) => (e) => {
    const a = keyAction(e, type)
    if (!a) return
    e.preventDefault(); e.stopPropagation()
    if (a.do === 'leave') { leavePicture(); return }
    if (a.do === 'paste') {
      const clip = navigator.clipboard
      if (!clip || !clip.readText) { setLive(l => (l ? { ...l, msg: 'This browser doesn’t let Primer read the clipboard.' } : l)); return }
      clip.readText().then((t) => {
        if (!t) return
        const c = capPaste(t)
        sendIn({ kind: 'text', text: c.text })
        if (c.cut) setLive(l => (l ? { ...l, msg: `Only the first ${c.text.length.toLocaleString()} characters were pasted.` } : l))
      }).catch(() => setLive(l => (l ? { ...l, msg: 'Primer wasn’t allowed to read the clipboard.' } : l)))
      return
    }
    if (a.do === 'text') sendIn({ kind: 'text', text: a.text })
    else if (a.do === 'key') sendIn(a.event)
  }
  // the wheel needs a listener that isn't passive, so the window itself doesn't scroll
  const showing = !!(live && !live.error && !live.gone)
  useEffect(() => {
    const el = picRef.current
    if (!showing || !el) return
    const wheel = (e) => {
      e.preventDefault()
      const p = pt(e, false)
      if (!p) return
      const { dx, dy } = wheelDelta(e, el.clientHeight || 600)
      if (dx || dy) sendIn({ kind: 'wheel', ...p, dx, dy, modifiers: modifiers(e) })
    }
    el.addEventListener('wheel', wheel, { passive: false })
    return () => el.removeEventListener('wheel', wheel)
  }, [showing])

  // the page follows the window's size: sent once it has stopped changing for RESIZE_MS
  useEffect(() => {
    const el = bodyRef.current
    if (!showing || !el || typeof ResizeObserver === 'undefined') return
    const m = liveRef.current
    if (!m) return
    let t = 0
    const ro = new ResizeObserver(() => {
      clearTimeout(t)
      t = setTimeout(() => {
        if (liveRef.current !== m || m.gone) return
        const w = liveSize(el.clientWidth), h = liveSize(el.clientHeight)
        if (m.size && m.size.w === w && m.size.h === h) return
        m.size = { w, h }
        api.liveResize(m.id, w, h).catch(() => {})
      }, RESIZE_MS)
    })
    ro.observe(el)
    return () => { clearTimeout(t); ro.disconnect() }
  }, [showing])

  /* ---- the address bar ------------------------------------------------------------------- */

  const show = (entry, mode) => {
    if (!entry) return
    if (entry.search) { setAddr(entry.search.q); search(entry.search.q, entry.search.where, mode) } else load(entry.u, mode)
  }
  const addrOf = (entry) => (entry.search ? entry.search.q : entry.u)
  const submit = (e) => {
    e.preventDefault()
    const r = addressFromInput(addr, st)
    if (live && !live.gone && !live.error && r.u && liveable(r.u)) { liveNavTo('url', r.u); return }
    if (live && (live.gone || live.error) && r.u && liveable(r.u)) { openLive(r.u); return }
    if (r.error) { if (live) setLive(l => ({ ...l, msg: r.error })); else { setError(r.error); setFailed(null) } return }
    if (r.u) load(r.u); else search(r.search, r.where)
  }
  const back = () => (live ? liveNavTo('back') : show(nav.current.peek(-1), 'back'))
  const forward = () => (live ? liveNavTo('forward') : show(nav.current.peek(1), 'forward'))
  const reload = () => {
    if (live) { if (live.gone || live.error) openLive(live.u, live.title); else liveNavTo('reload'); return }
    const c = nav.current.current(); if (c) show(c, 'replace')
  }

  /* a link on the page: here, or (ctrl/middle) a new Browser window */
  const follow = (u, { newWindow }) => { if (newWindow) openBrowser(u, { newWindow: true }); else load(u) }

  const first = useRef(false)
  useEffect(() => {
    if (!first.current) {
      const again = resume.get(windowId)
      if (again) { first.current = true; resume.delete(windowId); openLive(again.u, again.title) }
      else if (start) { first.current = true; load(start) }
    }
    return () => { first.current = false }    // StrictMode mounts twice: the second mount must load too
  }, [start, load])
  // another window asked the most recent Browser to open an address
  useEffect(() => {
    const on = (e) => {
      const d = e.detail
      if (!d || d.handled || d.windowId !== windowId) return
      d.handled = true
      const m = liveRef.current
      if (m && !m.gone && liveable(d.u)) liveNavTo('url', d.u); else load(d.u)
    }
    window.addEventListener(OPEN_BROWSER_EVENT, on)
    return () => window.removeEventListener(OPEN_BROWSER_EVENT, on)
  }, [windowId, load])
  useEffect(() => {
    if (live) { const t = live.title || live.u; if (t) wm.setTitle(windowId, t) }
    else if (view && view.title) wm.setTitle(windowId, view.title)
  }, [windowId, view, live])

  const page = view && view.markdown !== undefined ? view : null
  const cur = nav.current.current()
  // a reader page the site failed (blocked, rate-limited, needs scripts) can still be opened
  // live; Ask and Make a page wait for a page that is open
  const failedU = !live && !page && error ? failed : null
  const { cur: curU, live: liveU } = pageTargets({ live, page, failed: failedU })
  const curTitle = live ? live.title : page ? page.title : ''

  /* selection menu */
  const onSelect = () => {
    if (live) return
    const sel = window.getSelection()
    if (!sel || sel.isCollapsed || !sel.rangeCount || !bodyRef.current) return setMenu(null)
    const range = sel.getRangeAt(0)
    if (!bodyRef.current.contains(range.commonAncestorContainer)) return setMenu(null)
    const text = cleanSelection(sel.toString())
    if (!selectionMenuItems(text).length) return setMenu(null)
    const b = bodyRef.current.getBoundingClientRect(), r = range.getBoundingClientRect()
    setMenu({ text, x: clampX(r.left - b.left, 130, bodyRef.current.clientWidth), y: r.bottom - b.top + bodyRef.current.scrollTop + 6 })
  }
  const pickMenu = (id) => {
    const text = menu.text
    setMenu(null)
    if (id === 'explore') wm.open('reader', { props: { start: { concept: text, via: 'typed' } }, w: 720, h: 640 })
    else {
      setTab('ask'); setQuestion(askPrefill(text))
      setTimeout(() => { const i = askRef.current && askRef.current.querySelector('input'); if (i) i.focus() }, 0)
    }
  }

  const makePage = () => {
    if (!curU) return
    // a live page: the server reads its own text (source_live) and cites its address
    const m = live && liveRef.current
    const start = { concept: pageConcept(curTitle) || curU, via: 'typed', source_url: curU }
    if (m && !m.gone) start.source_live = m.id
    wm.open('reader', { props: { start }, w: 720, h: 640 })
  }

  /* Ask: stream an answer about this page */
  const runAsk = async (e) => {
    e.preventDefault()
    if (!curU || !question.trim() || ask.running) return
    stopAsk()
    const ac = new AbortController(); askAbort.current = ac
    const alive = () => askAbort.current === ac
    setAsk({ running: true, text: '', error: '' })
    try {
      const m = live && liveRef.current  // a live page: ask about its own text
      await api.browseAsk(curU, question.trim(), ({ event, data }) => {
        if (!alive()) return
        if (event === 'delta') setAsk(a => ({ ...a, text: a.text + (data.text || '') }))
        else if (event === 'error') setAsk(a => ({ ...a, error: data.message || 'The answer could not be written.' }))
      }, ac.signal, m && !m.gone ? m.id : undefined)
    } catch (err) {
      if (err.name === 'AbortError' || !alive()) return
      setAsk(a => ({ ...a, error: err.message || 'The answer could not be written.' }))
    }
    if (alive()) { askAbort.current = null; setAsk(a => ({ ...a, running: false })) }
  }

  /* Click through: Primer follows links toward the goal; each step moves this window */
  const runGo = async (e) => {
    e.preventDefault()
    if (live) return runLiveGo()
    if (!curU || !goal.trim() || go.running) return
    stopGo()
    const ac = new AbortController(); goAbort.current = ac
    const alive = () => goAbort.current === ac
    setGo({ running: true, steps: [], answer: '', note: '', error: '', waiting: false, ask: null })
    try {
      await api.browseAgent(curU, goal.trim(), ({ event, data }) => {
        if (!alive()) return
        if (event === 'step') {
          setGo(g => ({ ...g, waiting: false, steps: [...g.steps, data] }))
          { const u = stepTarget(data); if (u) load(u, 'push', true) }
        } else if (event === 'answer') setGo(g => ({ ...g, answer: data.text || '' }))
        else if (event === 'waiting') setGo(g => ({ ...g, waiting: true }))
        else if (event === 'done') setGo(g => ({ ...g, note: data.found === false && !g.answer ? 'I did not find it within the steps allowed.' : '' }))
        else if (event === 'error') setGo(g => ({ ...g, error: data.message || 'Click through stopped.' }))
      }, ac.signal)
    } catch (err) {
      if (err.name === 'AbortError' || !alive()) return
      setGo(g => ({ ...g, error: err.message || 'Click through stopped.' }))
    }
    if (alive()) { goAbort.current = null; setGo(g => ({ ...g, running: false, waiting: false })) }
  }

  /* Click through, live: the model reads the live page and acts on it; press, type and choose
   * wait for Allow once */
  const runLiveGo = async () => {
    const m = liveRef.current
    if (!m || m.gone || !goal.trim() || go.running) return
    stopGo()
    const ac = new AbortController(); goAbort.current = ac
    const alive = () => goAbort.current === ac
    setGo({ running: true, steps: [], answer: '', note: '', error: '', waiting: false, ask: null })
    try {
      await api.liveAgent(m.id, goal.trim(), ({ event, data }) => {
        if (!alive() || !data) return
        if (event === 'step') {
          setGo(g => ({ ...g, waiting: false, steps: [...g.steps, data] }))
          const a = data.action || {}
          if (a.action === 'follow' && !data.refused && liveable(a.href)) setLiveU(a.href, '')
          else if (data.saw && liveable(data.url)) { if (liveRef.current) Object.assign(liveRef.current, { u: data.url, title: data.saw }); setLive(l => (l ? { ...l, title: data.saw } : l)) }
        } else if (event === 'ask') { setTab('go'); setGo(g => ({ ...g, waiting: false, ask: data })) }
        else if (event === 'answer') setGo(g => ({ ...g, answer: data.text || '', ask: null }))
        else if (event === 'waiting') setGo(g => ({ ...g, waiting: true }))
        else if (event === 'stopped') setGo(g => ({ ...g, ask: null, note: data.reason === 'Stopped' ? 'Stopped.' : goneText(data.reason || 'Stopped') }))
        else if (event === 'error') setGo(g => ({ ...g, ask: null, error: data.message || 'Click through stopped.' }))
      }, ac.signal)
    } catch (err) {
      if (err.name === 'AbortError' || !alive()) return
      setGo(g => ({ ...g, error: err.message || 'Click through stopped.' }))
    }
    if (alive()) { goAbort.current = null; setGo(g => ({ ...g, running: false, waiting: false, ask: null })) }
  }
  const answer = async (allow) => {
    const m = liveRef.current, a = go.ask
    if (!m || !a) return
    setGo(g => ({ ...g, ask: null, note: allow ? '' : 'Stopped.' }))
    try { await answerLiveAsk(m.id, a, allow) } catch (e) {
      // the question went (timed out, or the page closed): Stop still ends the run
      if (!allow) { stopGo(); setGo(g => ({ ...g, running: false, waiting: false, ask: null })) }
      else setGo(g => ({ ...g, error: e.message || 'That answer didn’t arrive.' }))
    }
  }
  const stopClick = () => {
    if (live && go.ask) { answer(false); return }
    stopGo(); setGo(g => ({ ...g, running: false, waiting: false, ask: null, note: 'Stopped.' }))
  }

  /* the window's menu: Forget everything */
  useEffect(() => { if (wmenu && menuItemRef.current) menuItemRef.current.focus() }, [wmenu])
  const focusMenuButton = () => { const b = moreRef.current && moreRef.current.querySelector('button'); if (b) b.focus() }
  const closeForget = () => { setForget({ open: false, busy: false, error: '' }); setTimeout(focusMenuButton, 0) }
  // Tab stays inside the dialog while it is open
  const trapTab = (e) => {
    if (e.key !== 'Tab') return
    const bs = [...e.currentTarget.querySelectorAll('button:not([disabled])')]
    if (!bs.length) { e.preventDefault(); return }
    const i = bs.indexOf(document.activeElement)
    const next = e.shiftKey ? (i <= 0 ? bs.length - 1 : i - 1) : (i === -1 || i === bs.length - 1 ? 0 : i + 1)
    e.preventDefault(); bs[next].focus()
  }
  const doForget = async () => {
    setForget(f => ({ ...f, busy: true, error: '' }))
    try {
      await api.liveForget()
      closeForget()
      setForgotten('Primer’s browser has forgotten every sign-in, cookie and page it visited.')
    } catch (e) {
      setForget({ open: true, busy: false, error: e.message || 'That didn’t work.' })
    }
  }

  const ba = st && st.browse_available
  const unavailable = ba && !ba.ok
  const origin = !live && page && safeHref(page.origin_href)
  const ls = st && st.live
  const liveOn = !!(ls && ls.available && ls.enabled)
  const liveWhy = !ls ? '' : !ls.available ? 'Live pages need Chrome or Edge'
    : !ls.enabled ? 'Live pages are off while web search is off' : ''
  const liveDown = !!(live && (live.gone || live.error))

  return (
    <div className="bw">
      <form className="bw-bar" onSubmit={submit} ref={barRef}>
        <Button variant="ghost" size="sm" icon="back" aria-label="Back" title="Back"
                disabled={live ? liveDown || live.opening : !nav.current.canBack()} onClick={back} />
        <Button variant="ghost" size="sm" icon="forward" aria-label="Forward" title="Forward"
                disabled={live ? liveDown || live.opening : !nav.current.canForward()} onClick={forward} />
        <Button variant="ghost" size="sm" icon="reload" aria-label="Reload" title="Reload"
                disabled={live ? live.opening : !cur || loading} onClick={reload} />
        <Input className="bw-addr" value={addr} onChange={e => setAddr(e.target.value)} autoComplete="off" spellCheck="false"
               placeholder="Type an address or some words to look up" aria-label="Address" />
        {live ? <Chip tone="accent" dot>Live</Chip> : null}
        {live ? <Button size="sm" data-focus="reader" onClick={toReader} title="Show this address in the reader">Reader</Button> : null}
        {!live && page ? <Chip tone={page.where === 'reference' ? 'accent' : 'neutral'}>{page.where === 'reference' ? 'offline' : 'web'}</Chip> : null}
        {!live && ls ? (
          <Button size="sm" disabled={!liveOn || !liveable(liveU)} onClick={() => openLive(liveU, page && page.title)}
                  title={liveWhy || (liveable(liveU) ? 'Open this page live, with its scripts' : 'Only web pages open live')}>Live</Button>
        ) : null}
        {origin ? <a className="bw-orig" href={origin} target="_blank" rel="noopener noreferrer" title="Open the original in your own browser">Original</a> : null}
        <div className="bw-more" ref={moreRef}>
          <Button variant="ghost" size="sm" aria-label="Browser menu" title="Browser menu" aria-haspopup="menu"
                  aria-expanded={wmenu} onClick={() => setWmenu(o => !o)}>⋯</Button>
          {wmenu ? (
            <div className="bw-wmenu" role="menu" aria-label="Browser menu"
                 onKeyDown={(e) => { if (e.key === 'Escape') { e.preventDefault(); setWmenu(false); focusMenuButton() } }}>
              <button type="button" role="menuitem" ref={menuItemRef}
                      onClick={() => { setWmenu(false); setForget({ open: true, busy: false, error: '' }) }}>
                Forget everything (sign-ins, cookies, history)
              </button>
            </div>
          ) : null}
        </div>
      </form>

      {unavailable ? <div className="bw-note" role="status">{ba.reason}</div> : null}
      {forgotten ? <div className="bw-note" role="status">{forgotten}</div> : null}

      <div className="bw-main">
        <div className={'bw-content' + (live ? ' bw-content-live' : '')} ref={bodyRef} onMouseUp={onSelect}
             onScroll={() => menu && setMenu(null)}>
          {live ? (
            <>
              {live.error ? <State kind="error" title="Couldn’t open it live">{live.error}</State> : null}
              {live.gone ? (
                <State kind="empty" title="The live page has closed"
                       action={<Button size="sm" onClick={() => openLive(live.u, live.title)}>Open it live again</Button>}>{live.gone}</State>
              ) : null}
              {!liveDown ? (
                <div className="bw-live" ref={picRef} tabIndex={0} role="img"
                     aria-label={`Live page: ${live.title || live.u}`} aria-describedby={hintId}
                     onPointerDown={onPicDown} onPointerMove={onPicMove} onPointerUp={onPicUp} onPointerCancel={onPicUp}
                     onLostPointerCapture={onPicUp} onContextMenu={e => e.preventDefault()}
                     onKeyDown={onPicKey('down')} onKeyUp={onPicKey('up')}>
                  <img ref={imgRef} alt="" draggable={false} className={'bw-live-img' + (live.framed ? '' : ' bw-live-wait')} />
                  {!live.framed ? <div className="bw-live-opening"><Chip busy tone="accent">Opening live…</Chip></div> : null}
                </div>
              ) : null}
              {!liveDown ? (
                <div className="bw-live-hint">
                  <span id={hintId}>Clicks, scrolling and typing go to the page. Shift+Esc leaves it.</span>
                  <button type="button" className="bw-live-leave" onClick={leavePicture}>Leave the page</button>
                </div>
              ) : null}
              {!liveDown && (live.note || live.msg) ? (
                <div className="bw-live-note" role="status">{live.msg || live.note}</div>
              ) : null}
            </>
          ) : (
            <>
              {loading && !view ? <State kind="loading" title="Opening…" /> : null}
              {error ? <State kind="error" title="Couldn’t open that">{error}</State> : null}
              {failedU && !loading && liveOn ? (
                <div className="bw-offer" role="status">
                  The reader couldn’t open this page.{' '}
                  <Button size="sm" onClick={() => openLive(failedU)}>Open it live</Button>
                </div>
              ) : null}
              {page && !loading && liveOn && liveable(page.u) && offerLive(page.markdown) ? (
                <div className="bw-offer" role="status">
                  This page needs its scripts.{' '}
                  <Button size="sm" onClick={() => openLive(page.u, page.title)}>Open it live</Button>
                </div>
              ) : null}
              {page ? (
                <article className={'bw-article' + (loading ? ' bw-dim' : '')}>
                  <Markdown source={page.markdown} onLink={follow} />
                </article>
              ) : null}
              {view && view.results ? (
                <div className={'bw-results' + (loading ? ' bw-dim' : '')}>
                  <h2>{view.results.length ? `Results for “${view.search}” (${whereName(view.where)})` : `Nothing found for “${view.search}” (${whereName(view.where)})`}</h2>
                  <ul>
                    {view.results.map(r => (
                      <li key={r.url}>
                        <a href={r.url} onClick={e => { e.preventDefault(); if (e.ctrlKey || e.metaKey) openBrowser(r.url, { newWindow: true }); else load(r.url) }}>{r.title || r.url}</a>
                        {r.snippet ? <p>{r.snippet}</p> : null}
                      </li>
                    ))}
                  </ul>
                </div>
              ) : null}
              {!view && !loading && !error ? <div className="bw-empty">Type an address, or some words to look up.</div> : null}
              {menu ? (
                <div className="bw-menu" role="menu" aria-label="Selection" style={{ left: menu.x, top: menu.y }}
                     onMouseDown={e => e.preventDefault()}>
                  {selectionMenuItems(menu.text).map(i => (
                    <Button key={i.id} size="sm" role="menuitem" onClick={() => pickMenu(i.id)}>{i.label}</Button>
                  ))}
                </div>
              ) : null}
            </>
          )}
        </div>

        <aside className="bw-side" aria-label="Ask and click through">
          <div className="bw-tabs" role="tablist">
            <button type="button" role="tab" aria-selected={tab === 'ask'} className={tab === 'ask' ? 'on' : ''} onClick={() => setTab('ask')}>Ask</button>
            <button type="button" role="tab" aria-selected={tab === 'go'} className={tab === 'go' ? 'on' : ''} onClick={() => setTab('go')}>Click through</button>
          </div>
          {tab === 'ask' ? (
            <div className="bw-pane">
              <form onSubmit={runAsk} className="bw-form" ref={askRef}>
                <Input value={question} onChange={e => setQuestion(e.target.value)} disabled={!curU}
                       placeholder="Ask about this page…" aria-label="Question about this page" autoComplete="off" />
                {ask.running
                  ? <Button size="sm" onClick={() => { stopAsk(); setAsk(a => ({ ...a, running: false })) }} type="button">Stop</Button>
                  : <Button size="sm" variant="primary" type="submit" disabled={!curU || !question.trim()}>Ask</Button>}
              </form>
              {ask.text ? <div className="bw-answer"><Markdown source={ask.text} /></div> : null}
              {ask.running && !ask.text ? <Chip busy tone="accent">reading…</Chip> : null}
              {ask.error ? <div className="bw-err" role="alert">{ask.error}</div> : null}
            </div>
          ) : (
            <div className="bw-pane">
              <form onSubmit={runGo} className="bw-form">
                <Input value={goal} onChange={e => setGoal(e.target.value)} disabled={(live ? liveDown || live.opening : !page) || go.running}
                       placeholder="What should I find?" aria-label="Goal" autoComplete="off" />
                {go.running
                  ? <Button size="sm" onClick={stopClick} type="button">Stop</Button>
                  : <Button size="sm" variant="primary" type="submit"
                            disabled={(live ? liveDown || live.opening : !page) || !goal.trim()}>Start</Button>}
              </form>
              <div className="bw-live-region" aria-live="polite">
                {live ? <LiveSteps steps={go.steps} /> : go.steps.length ? (
                  <ol className="bw-steps">
                    {go.steps.map(s => (
                      <li key={s.n}>
                        <div className="bw-step-t">{s.title || s.url}</div>
                        {stepLink(s) ? <div className="bw-step-a">followed “{stepLink(s)}”</div> : null}
                        {s.why ? <div className="bw-step-w">{s.why}</div> : null}
                      </li>
                    ))}
                  </ol>
                ) : null}
                {live && go.ask ? <AskPrompt ask={go.ask} onAnswer={answer} /> : null}
                {go.running && !go.ask ? <Chip busy tone="accent">{go.waiting ? 'waiting for the storyteller…' : 'looking…'}</Chip> : null}
                {go.answer ? <div className="bw-answer"><Markdown source={go.answer} /></div> : null}
                {go.note ? <div className="bw-note-s">{go.note}</div> : null}
              </div>
              {go.error ? <div className="bw-err" role="alert">{go.error}</div> : null}
            </div>
          )}
          <div className="bw-foot">
            <Button size="sm" disabled={!curU} onClick={makePage}
                    title="Write a Primer page from this page alone">Make a page</Button>
          </div>
        </aside>
      </div>

      {forget.open ? (
        <div className="bw-dlg-back">
          <div className="bw-dlg" role="alertdialog" aria-modal="true" aria-labelledby={hintId + '-fh'} aria-describedby={hintId + '-fd'}
               onKeyDown={(e) => { if (e.key === 'Escape' && !forget.busy) { e.preventDefault(); closeForget() } else trapTab(e) }}>
            <h2 id={hintId + '-fh'}>Forget everything?</h2>
            <p id={hintId + '-fd'}>Primer’s browser will forget every sign-in, cookie and page it has visited. Any live page open now closes.</p>
            {forget.error ? <div className="bw-err" role="alert">{forget.error}</div> : null}
            <div className="bw-dlg-b">
              <Button size="sm" variant="danger" disabled={forget.busy} onClick={doForget}>{forget.busy ? 'Forgetting…' : 'Forget everything'}</Button>
              <Button size="sm" autoFocus disabled={forget.busy} onClick={closeForget}>Cancel</Button>
            </div>
          </div>
        </div>
      ) : null}
    </div>
  )
}
