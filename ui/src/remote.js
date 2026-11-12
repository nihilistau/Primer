/* remote — Kairos drives Primer (Phase 15), the window's side.
 *
 * createListener holds this tab's GET /v1/tool/stream open: the first event, `hello`, names the
 * tab's listener id; each `command` after it ({id, kind: show|open|lens, args}) goes to
 * onCommand(command, finish), and finish({ok, result|error}) posts it done once. The tab's
 * focused page goes to POST /v1/tool/focus through reportFocus (and again after a reconnect,
 * since a new stream is a new listener) — only while this tab is visible, unless the caller says
 * `always` (the tab just gained focus) or this tab ran Kairos's last command; a report made while
 * the tab is hidden says so (`hidden`), and the server then won't make this the focused tab: a
 * background tab must not take Kairos's commands from the one in front. A dropped stream
 * reconnects after 1 s, doubling to 10 s.
 *
 * The stream is held only while "Let Kairos drive Primer" is on (setEnabled; watchRemoteEnabled
 * reads it from /v1/status every few seconds, and at once when this tab flips it): a browser
 * allows six connections per site, so a stream in every tab would stall the tabs' other requests.
 * The server refuses the stream (403) while it is off and ends them all when it goes off; a
 * refused tab waits for the status to say on again rather than retrying.
 *
 * The rest is the Reader's side: which Reader window takes a command (the most recently
 * focused one, else a new one), and pure helpers that turn a command into the Reader's own
 * actions, so a Kairos-made page is an ordinary page. */
import * as api from './api.js'
import { parseSSE } from './sse.js'
import { diagramButton, imageButton, mapButton, storyButton, timelineButton } from './reader/imageRoute.js'
import { remoteEnabled } from './apps/thinkingAhead.js'

export const BACKOFF_MIN_MS = 1000
export const BACKOFF_MAX_MS = 10000
export const NOTE_MS = 8000   // how long "Kairos asked: …" stays at the top of the desktop
export const REMOTE_POLL_MS = 5000   // how often a tab reads the switch from /v1/status

/* the wait before the next reconnect, after waiting `ms` (0: the first try after a good stream) */
export const nextDelay = (ms) => Math.min(BACKOFF_MAX_MS, ms ? ms * 2 : BACKOFF_MIN_MS)

/* is this tab showing? (no document, as in tests: yes) */
export const tabVisible = () => typeof document === 'undefined' || document.visibilityState === 'visible'

export function createListener({ fetch = (...a) => globalThis.fetch(...a), onCommand = () => {}, onState = () => {},
                                 focus = api.toolFocus, done = api.toolDone, visible = tabVisible, enabled = true } = {}) {
  let closed = false
  let on = !!enabled   // the switch, as this tab last saw it: no stream while off
  let gen = 0          // bumped when the stream is stopped: an older connect ends quietly
  let ac = null
  let timer = null
  let delay = 0
  let listener = null
  let last = null   // the last focus reported, sent again to each new listener
  let owner = false // this tab ran Kairos's last command: its page changes count even while hidden

  const sendFocus = (always) => {
    if (!listener || !last) return
    const shown = visible()
    if (!(always || owner || shown)) return
    // hidden: the server keeps this tab's page but doesn't make it the focused tab
    const sent = shown ? focus(listener, last.nodeId, last.title) : focus(listener, last.nodeId, last.title, true)
    Promise.resolve(sent).catch(() => {})
  }

  const command = (cmd, lis) => {
    let sent = false
    owner = true
    const finish = (outcome) => {
      if (sent || closed) return Promise.resolve()
      sent = true
      return Promise.resolve(done(cmd.id, lis, outcome)).catch(() => {})
    }
    try {
      const r = onCommand(cmd, finish)
      if (r && typeof r.catch === 'function') {
        r.catch(e => finish({ ok: false, error: (e && e.message) || 'Primer could not do that' }))
      }
    } catch (e) {
      finish({ ok: false, error: (e && e.message) || 'Primer could not do that' })
    }
  }

  const connect = async () => {
    timer = null
    const g = gen
    const stale = () => closed || g !== gen
    if (stale() || !on) return
    const mine = new AbortController()
    ac = mine
    let greeted = false
    let off = false
    try {
      const r = await fetch('/v1/tool/stream', { headers: { 'X-Primer': '1' }, signal: mine.signal })
      off = r.status === 403
      if (!r.ok || !r.body) throw new Error(`tool stream: ${r.status}`)
      const reader = r.body.getReader()
      const dec = new TextDecoder()
      let carry = ''
      try {
        for (;;) {
          const { done: end, value } = await reader.read()
          if (stale()) break
          const out = parseSSE(end ? dec.decode() : dec.decode(value, { stream: true }), carry)
          carry = out.carry
          for (const { event, data } of out.events) {
            if (stale()) break
            if (event === 'hello' && data && typeof data.listener === 'string' && data.listener) {
              listener = data.listener
              greeted = true
              delay = 0
              onState({ connected: true, listener })
              sendFocus()
            } else if (event === 'command' && listener && data && typeof data.id === 'string') {
              command(data, listener)
            }
          }
          if (end) break
        }
      } finally {
        reader.cancel().catch(() => {})
      }
    } catch { /* refused, dropped or aborted: reconnect below unless closed or switched off */ }
    if (stale()) return
    ac = null
    listener = null
    owner = false
    onState({ connected: false })
    if (off) { on = false; return }   // the switch is off: wait for the status to say on (setEnabled)
    delay = nextDelay(greeted ? 0 : delay)
    timer = setTimeout(connect, delay)
  }

  const stop = () => {
    gen += 1
    if (timer) clearTimeout(timer)
    timer = null
    if (ac) ac.abort()
    ac = null
  }

  connect()

  return {
    close() {
      closed = true
      stop()
    },
    /* the switch, from /v1/status or this tab's Engines window: on connects (unless connected
     * or waiting to retry already), off ends the stream and every retry */
    setEnabled(value) {
      if (closed) return
      if (!value) {
        on = false
        stop()
        if (listener) onState({ connected: false })
        listener = null
        owner = false
        return
      }
      if (on && (ac || timer)) return
      on = true
      stop()
      delay = 0
      connect()
    },
    /* the focused Reader's page; sent now when connected (and visible, or `always`), and again
     * after each reconnect while visible */
    /* whether this tab ran Kairos's last command (since its stream connected) */
    owns() { return owner },
    reportFocus(nodeId, title, { always = false } = {}) {
      if (!nodeId || !title) return
      last = { nodeId, title }
      sendFocus(always)
    },
  }
}

/* THE SWITCH, as this tab sees it: listener.setEnabled(on) from /v1/status every REMOTE_POLL_MS,
 * and at once when this tab's Engines window flips it (remoteSwitched). Returns the cleanup. */
const switchWatchers = new Set()
export const remoteSwitched = (on) => switchWatchers.forEach(f => f(!!on))

export function watchRemoteEnabled({ listener, status = api.status, every = REMOTE_POLL_MS }) {
  let live = true
  let timer = null
  const tick = async () => {
    timer = null
    let st = null
    try { st = await status() } catch { /* Primer restarting: keep what we have, ask again */ }
    if (!live) return
    if (st) listener.setEnabled(remoteEnabled(st))
    timer = setTimeout(tick, every)
  }
  const flipped = (on) => listener.setEnabled(on)
  switchWatchers.add(flipped)
  tick()
  return () => { live = false; clearTimeout(timer); switchWatchers.delete(flipped) }
}

/* WHICH READER. Each Reader window registers a handler and reports its page; a command goes
 * to the registered Reader whose window is on top (the most recently focused), or, with none,
 * to the next Reader to register (the caller opens one). */
const readers = new Map()   // windowId -> { handle(cmd, finish), nodeId, title }
const watchers = new Set()
let pending = null          // {cmd, finish} waiting for a Reader to open

const changed = () => watchers.forEach(f => f())
export const subscribeReaders = (f) => { watchers.add(f); return () => watchers.delete(f) }

export function registerReader(windowId, handle) {
  readers.set(windowId, { handle, nodeId: null, title: '' })
  if (pending) {
    const { cmd, finish } = pending
    pending = null
    handle(cmd, finish)
  }
  changed()
  return () => { if (readers.get(windowId)?.handle === handle) { readers.delete(windowId); changed() } }
}

export function setReaderPage(windowId, nodeId, title) {
  const r = readers.get(windowId)
  if (!r || (r.nodeId === nodeId && r.title === title)) return
  r.nodeId = nodeId || null
  r.title = title || ''
  changed()
}

/* the id of the top (highest z) showing window in `windows` that is a registered Reader, or null;
 * a minimised (or minimising) one is skipped, as Desktop skips it for the focused window */
export function topReader(windows, registered = readers) {
  let top = null
  for (const w of windows) {
    if (registered.has(w.id) && !w.minimized && !w.minimizing && (!top || w.z > top.z)) top = w
  }
  return top ? top.id : null
}

/* {nodeId, title} of the most recently focused Reader, or null when it shows no page */
export function focusedPage(windows, registered = readers) {
  const id = topReader(windows, registered)
  const r = id && registered.get(id)
  return r && r.nodeId && r.title ? { nodeId: r.nodeId, title: r.title } : null
}

/* Hand a command to the most recently focused Reader, or open one for it (openReader). */
export function dispatchCommand(cmd, finish, { windows, openReader }) {
  const id = topReader(windows)
  if (id) { readers.get(id).handle(cmd, finish); return id }
  if (pending) pending.finish({ ok: false, error: 'Primer was asked for something else first' })
  pending = { cmd, finish }
  openReader()
  return null
}

/* test isolation */
export function _resetReaders() { readers.clear(); watchers.clear(); pending = null }

/* Report the focused Reader's page to `listener`: when it changes (deduped, and only while the
 * tab is visible) and whenever the tab gains focus (always, even unchanged: the server sends
 * each command to the tab that reported focus last). Returns the cleanup. */
export function watchFocus({ listener, getWindows, subscribeWindows, subscribe = subscribeReaders,
                             win = window, visible = tabVisible }) {
  let last = ''
  const report = (always) => {
    const p = focusedPage(getWindows())
    if (!p) return
    const key = `${p.nodeId} ${p.title}`
    if (!always && (key === last || !(visible() || (listener.owns && listener.owns())))) return
    last = key
    listener.reportFocus(p.nodeId, p.title, { always })
  }
  const offWindows = subscribeWindows(() => report(false))
  const offReaders = subscribe(() => report(false))
  const onFocus = () => report(true)
  win.addEventListener('focus', onFocus)
  return () => { offWindows(); offReaders(); win.removeEventListener('focus', onFocus) }
}

/* WHAT A COMMAND MEANS. */
/* the words after "Kairos asked: " for a lens, given the page's title (the server adds it to the
 * command's args); without one, "this page" */
const LENS_NOTE = {
  picture: (t) => `a picture of ${t}`, map: (t) => `a map of ${t}`, timeline: (t) => `a timeline of ${t}`,
  diagram: (t) => `a diagram of ${t}`, story: (t) => `a story about ${t}`, listen: (t) => `to read ${t} aloud`,
}
export const LENSES = Object.keys(LENS_NOTE)

/* the words after "Kairos asked: " */
export function noteText(cmd) {
  const a = (cmd && cmd.args) || {}
  const title = typeof a.title === 'string' ? a.title.trim() : ''
  if (cmd && cmd.kind === 'show') return String(a.text || '').trim()
  if (cmd && cmd.kind === 'open') return title ? `open ${title}` : 'open a page you read before'
  if (cmd && cmd.kind === 'lens') return LENS_NOTE[a.kind] ? LENS_NOTE[a.kind](title || 'this page') : 'a lens on this page'
  return 'something Primer does not know'
}

/* the desktop's note for a command: {text, key}; the key restarts its fade for each command */
export const kairosNote = (cmd, key = Date.now()) => ({ text: noteText(cmd), key })

/* show {text} | open {nodeId} | lens {lens, nodeId, here (the Reader already shows it)} |
 * {error} for a command this window can't run */
export function planCommand(cmd, focusedNodeId) {
  const a = (cmd && cmd.args) || {}
  if (cmd && cmd.kind === 'show' && typeof a.text === 'string' && a.text.trim()) return { type: 'show', text: a.text.trim() }
  if (cmd && cmd.kind === 'open' && typeof a.id === 'string' && a.id) return { type: 'open', nodeId: a.id }
  if (cmd && cmd.kind === 'lens' && LENSES.includes(a.kind)) {
    const nodeId = typeof a.id === 'string' && a.id ? a.id : focusedNodeId
    if (!nodeId) return { error: 'Open a page in Primer first' }
    return { type: 'lens', lens: a.kind, nodeId, here: nodeId === focusedNodeId }
  }
  return { error: 'Primer does not know that command' }
}

/* routePrompt's answer, made Kairos's: the page request carries via 'kairos' in place of
 * 'typed' (only this request: a later reopen of the page uses the normal via) */
export function kairosRoute(route) {
  if (!route || !route.body || route.body.via !== 'typed') return route
  return { ...route, body: { ...route.body, via: 'kairos' } }
}

/* the result.what Kairos speaks from, for each routePrompt kind; a page already read (the node
 * event's `cached`) was opened, not written */
const SHOW_WHAT = { page: 'writing', image: 'picture', video: 'video', map: 'map', timeline: 'timeline', diagram: 'diagram', story: 'story' }
export const showWhat = (kind, cached = false) =>
  ((!kind || kind === 'page' || !SHOW_WHAT[kind]) && cached ? 'opened' : SHOW_WHAT[kind] || 'writing')

/* the done outcome for a command that started */
export const started = (nodeId, title, what) =>
  ({ ok: true, result: { ...(nodeId ? { node_id: nodeId } : {}), title: (title && String(title).trim()) || 'this page', what } })
export const refused = (error) => ({ ok: false, error: error || 'Primer could not do that' })

/* {enabled, reason} for a lens from a /v1/status reply: the header button's own state */
export function lensButton(kind, status) {
  if (kind === 'picture') return imageButton(status)
  if (kind === 'map') return mapButton(status)
  if (kind === 'timeline') return timelineButton(status)
  if (kind === 'diagram') return diagramButton(status)
  return storyButton(status)   // story and listen
}

/* the Reader handler a lens button calls, with its arguments: [make, ...args] where make is
 * image | map | timeline | diagram | story */
export function lensCall(kind, nodeId, title) {
  if (kind === 'picture') return ['image', { node_id: nodeId, subject: title }]
  if (kind === 'map') return ['map', { node_id: nodeId, subject: title }]
  if (kind === 'timeline') return ['timeline', nodeId, title]
  if (kind === 'diagram') return ['diagram', { node_id: nodeId, subject: title }]
  if (kind === 'listen') return ['story', nodeId, 'listen', title]
  return ['story', nodeId, 'story', title]
}

/* CARRY ONE OUT. Runs a command through the Reader's own actions and calls finish exactly once:
 * when the page starts streaming (run's hooks.started(nodeId, title, cached)) or the lens job
 * starts, or with why not. deps, from the Reader:
 *   nodeId, title, streaming       what the Reader shows now
 *   routePrompt(text, {nodeId})    the ask box's router (imageRoute.js)
 *   carryOut(route, hooks?)        the ask box's branches; an image or video answers '' or an error
 *   run(body, mode, after, hooks)  open a page; after(nodeId, title) once it is written
 *   lens(kind, nodeId, title)      the header button's handler; answers '' or an error
 *   status()                       /v1/status, for the button's availability */
export async function kairosRun(cmd, deps, finish) {
  let sent = false
  const once = (outcome) => { if (!sent) { sent = true; finish(outcome) } }
  const failed = (msg) => once(refused(msg))
  const plan = planCommand(cmd, deps.nodeId)
  if (plan.error) return failed(plan.error)
  try {
    if (plan.type === 'show') {
      // the ask box's own path, with via 'kairos' on this page request only
      const r = kairosRoute(await deps.routePrompt(plan.text, { nodeId: deps.nodeId }))
      if (r.kind === 'image' || r.kind === 'video') {
        const err = await deps.carryOut(r)
        once(err ? refused(err) : started(r.body.node_id, r.body.subject, showWhat(r.kind)))
      } else {
        await deps.carryOut(r, { started: (id, title, cached) => once(started(id, title, showWhat(r.kind, cached))), failed })
      }
    } else if (plan.type === 'open') {   // as the trail opens a page: by id, never rewritten
      await deps.run({ node_id: plan.nodeId }, 'push', undefined,
                     { started: (id, title) => once(started(id, title, 'opened')), failed })
    } else {
      // a lens: the page first when the Reader shows another, then the button's own handler
      const press = async (id, title) => {
        let st
        try { st = await deps.status() } catch { return failed(lensButton(plan.lens, null).reason) }
        const b = lensButton(plan.lens, st)
        if (!b.enabled) return failed(b.reason)
        const err = await deps.lens(plan.lens, id, title)
        return once(err ? refused(err) : started(id, title, plan.lens))
      }
      if (plan.here && deps.streaming) failed('Primer is still writing this page')
      else if (plan.here) await press(plan.nodeId, deps.title)
      else {
        let pressing = null
        await deps.run({ node_id: plan.nodeId }, 'push', (id, title) => { pressing = press(id, title) },
                       { started: () => {}, failed })
        if (pressing) await pressing
        else failed('Primer could not open that page')
      }
    }
  } catch (e) {
    failed((e && e.message) || 'Primer could not do that')
  }
  failed('Primer could not do that')   // nothing said started or failed: never leave Kairos waiting
}
