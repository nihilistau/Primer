// adapted from shannon-prime-kairos/ui/src/windowManager.js
/* windowManager — a plain module-level store, deliberately trivial: an array of window
 * states plus a listener Set, driven through useSyncExternalStore. No context, no reducer.
 *
 * MULTI-INSTANCE: a window is found by its `id` (`${appId}:${n}`, n counting per app), not
 * by its appId. A `single` app (Graph, Engines) reuses its one window; any other app gets a
 * new instance on every open(). Geometry stays out of this module: it stores flags and the
 * pre-maximise box, and Window.jsx decides what "maximised" looks like.
 */
let windows = []
let z = 10
const counters = new Map()
const listeners = new Set()
const timers = new Map()

const emit = () => { windows = [...windows]; listeners.forEach(f => f()) }
const find = (id) => windows.find(w => w.id === id)

export const subscribe = (f) => { listeners.add(f); return () => listeners.delete(f) }
export const getWindows = () => windows

function stopTimer(id) { const t = timers.get(id); if (t) { clearTimeout(t); timers.delete(id) } }

/* Test isolation (R5): forget every window, counter and pending minimise timer. */
export function _reset() {
  timers.forEach(t => clearTimeout(t)); timers.clear()
  counters.clear(); windows = []; z = 10; emit()
}

/* Open an instance of appId and return its id. opts: { props, single, title, w, h }. */
export function open(appId, opts = {}) {
  if (opts.single) {
    const found = windows.find(w => w.appId === appId)
    if (found) {
      stopTimer(found.id)
      if (found.minimized) found.restoredAt = Date.now()
      found.minimized = false; found.minimizing = false
      if (opts.props) found.props = { ...found.props, ...opts.props }
      found.z = ++z; emit(); return found.id
    }
  }
  const n = (counters.get(appId) || 0) + 1
  counters.set(appId, n)
  const id = `${appId}:${n}`
  windows.push({
    id, appId, props: opts.props || {}, title: opts.title || '',
    z: ++z, minimized: false,
    // stagger, clear of the screen's left edge
    x: 132 + (windows.length % 6) * 34,
    y: 50 + (windows.length % 6) * 28,
    w: opts.w || 620, h: opts.h || 460,
  })
  emit()
  return id
}

/* Does this app have a window — minimised included? (The dock's dot.) */
export const isOpen = (appId) => windows.some(w => w.appId === appId)
/* Is a maximised window SHOWING? A maximised window that is minimised covers nothing. */
export const anyMaximized = () => windows.some(w => w.max && !w.minimized)

export const close = (id) => { stopTimer(id); windows = windows.filter(w => w.id !== id); emit() }

export function focus(id) {
  const w = find(id)
  if (!w) return
  stopTimer(id)
  if (w.minimized) w.restoredAt = Date.now()
  w.minimized = false; w.minimizing = false
  w.z = ++z; emit()
}

export function setTitle(id, title) {
  const w = find(id)
  if (!w || w.title === title) return
  w.title = title; emit()
}

/* MINIMISE ANIMATES. The window stays on the page, marked `minimizing`, for MIN_MS while the
 * shell flies it toward its dock button, then it is minimised. This module holds the flag
 * and the timer, never geometry. A caller that wants no flight passes delay 0. */
export const MIN_MS = 200

export function minimize(id, { delay = MIN_MS } = {}) {
  const w = find(id)
  if (!w || w.minimized || w.minimizing) return
  if (!delay) { w.minimized = true; emit(); return }
  w.minimizing = true
  emit()
  timers.set(id, setTimeout(() => {
    timers.delete(id)
    const cur = find(id)
    if (cur && cur.minimizing) { cur.minimizing = false; cur.minimized = true; emit() }
  }, delay))
}

/* MAXIMISE — a toggle that REMEMBERS the box the user arranged. */
export function maximize(id) {
  const w = find(id)
  if (!w) return
  // a maximise mid-flight cancels the minimise
  stopTimer(id)
  if (w.max) { Object.assign(w, w.pre || {}, { max: false, pre: null }) }
  else { w.pre = { x: w.x, y: w.y, w: w.w, h: w.h }; w.max = true }
  w.minimized = false; w.minimizing = false
  w.z = ++z
  emit()
}

export function moveResize(id, patch) {
  const w = find(id)
  if (!w) return
  // dragging a maximised window restores it first
  if (w.max) { Object.assign(w, w.pre || {}, { max: false, pre: null }) }
  Object.assign(w, patch)
  emit()
}
