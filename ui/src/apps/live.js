/* live — the Browser window's Live mode, the pure parts: window ids, mapping a pointer into the
 * frame's own pixels, mapping keys, the outgoing input queue (one move per animation frame,
 * typed text combined), the ask's words, a step's words, and when to offer "Open it live". */

export const WINDOW_ID = /^[a-z0-9-]{1,32}$/
export const MAX_TEXT = 1000          // characters of typed text in one post (the server's limit)
const MAX_TEXT_BYTES = 3500           // a text post's JSON stays well under the 4 KB input limit
export const MAX_PASTE = 20000       // characters of one paste; more is cut, with a note
export const CLICK_MS = 400          // a second press this soon after the last one, and
export const CLICK_PX = 4            // this near it, with the same button, counts as a double click
export const HOVER_MS = 66            // hover moves: about 15 a second

/* a live window's id: "live-" and a short random lowercase id */
export function newWindowId(rand = Math.random) {
  let s = ''
  while (s.length < 10) s += Math.floor(rand() * 36).toString(36)
  return 'live-' + s.slice(0, 10)
}

/* The frame is drawn with object-fit: contain inside `rect` ({left, top, width, height}), so it
 * may have bars at the sides or above and below. A client point -> {x, y} in the frame's own
 * pixels (0..fw, 0..fh), or null when the point is on a bar or outside. With `clamp`, a point
 * outside is pulled onto the frame's edge (a drag that leaves the picture still ends). */
export function framePoint(clientX, clientY, rect, fw, fh, clamp = false) {
  if (!rect || !(fw > 0) || !(fh > 0) || !(rect.width > 0) || !(rect.height > 0)) return null
  const scale = Math.min(rect.width / fw, rect.height / fh)
  const dw = fw * scale, dh = fh * scale
  const ox = rect.left + (rect.width - dw) / 2, oy = rect.top + (rect.height - dh) / 2
  let x = (clientX - ox) / scale, y = (clientY - oy) / scale
  if (!clamp && (x < 0 || y < 0 || x > fw || y > fh)) return null
  x = Math.max(0, Math.min(fw, x)); y = Math.max(0, Math.min(fh, y))
  return { x: Math.round(x * 10) / 10, y: Math.round(y * 10) / 10 }
}

/* the DevTools modifier bits: Alt 1, Ctrl 2, Meta 4, Shift 8 */
export const modifiers = (e) =>
  (e.altKey ? 1 : 0) | (e.ctrlKey ? 2 : 0) | (e.metaKey ? 4 : 0) | (e.shiftKey ? 8 : 0)

export const BUTTONS = ['left', 'middle', 'right']
export const mouseButton = (b) => BUTTONS[b] || 'left'

/* a wheel event's deltas in pixels (deltaMode 1 is lines, 2 is pages) */
export function wheelDelta(e, pageHeight = 600) {
  const k = e.deltaMode === 1 ? 16 : e.deltaMode === 2 ? pageHeight : 1
  const c = (d) => Math.max(-5000, Math.min(5000, Math.round((Number(d) || 0) * k)))
  return { dx: c(e.deltaX), dy: c(e.deltaY) }
}

/* the named keys the server knows (its _KEYS) */
export const NAMED_KEYS = new Set(['Enter', 'Tab', 'Backspace', 'Delete', 'Escape', 'ArrowLeft', 'ArrowUp',
  'ArrowRight', 'ArrowDown', 'Home', 'End', 'PageUp', 'PageDown'])

/* What a keyboard event on the focused live picture means:
 *   {do: 'leave'}           Shift+Esc: leave the picture (focus goes back to the window's bar)
 *   {do: 'paste'}           ctrl/cmd+V: read the clipboard and send it as text
 *   {do: 'text', text}      a printable character, typed (keydown only)
 *   {do: 'key', event}      a named key (Esc included) or a shortcut, sent as a key event (down
 *                           and up)
 *   {do: 'swallow'}         the keyup of typed text, a paste or Shift+Esc: kept from Primer
 *   null                    not for the page: let it be (F-keys, lone modifiers, Alt+Tab...)
 * `type` is 'down' or 'up'. AltGr arrives as Ctrl+Alt with a printable key: that's text. */
export function keyAction(e, type = 'down') {
  const key = e && e.key
  if (typeof key !== 'string' || e.isComposing) return null
  if (key === 'Escape' && e.shiftKey && !e.ctrlKey && !e.altKey && !e.metaKey) {
    return type === 'down' ? { do: 'leave' } : { do: 'swallow' }
  }
  const mods = modifiers(e)
  const altGr = e.ctrlKey && e.altKey && !e.metaKey
  const shortcut = (e.ctrlKey || e.metaKey || e.altKey) && !altGr
  if (shortcut && !e.altKey && (key === 'v' || key === 'V')) return type === 'down' ? { do: 'paste' } : { do: 'swallow' }
  if (NAMED_KEYS.has(key)) return { do: 'key', event: { kind: 'key', type, key, modifiers: mods } }
  if ([...key].length === 1 && !/[\u0000-\u001f\u007f]/.test(key)) {
    if (shortcut) return { do: 'key', event: { kind: 'key', type, key, modifiers: mods } }
    return type === 'down' ? { do: 'text', text: key } : { do: 'swallow' }
  }
  return null
}

/* Counts presses for double and triple clicks. Pointer events carry no click count on
 * pointerdown, so it is counted here: a press within CLICK_MS of the last one, within CLICK_PX
 * of it and with the same button counts one more (up to 3); anything else starts again at 1.
 * press(x, y, button, t) -> 1, 2 or 3 (x, y in client pixels; t in ms). */
export function createClickCounter(ms = CLICK_MS, px = CLICK_PX) {
  let last = null
  return {
    press(x, y, button, t) {
      const again = last && last.button === button && t - last.t <= ms
        && Math.abs(x - last.x) <= px && Math.abs(y - last.y) <= px
      const n = again ? Math.min(3, last.n + 1) : 1
      last = { x, y, button, t, n }
      return n
    },
    reset() { last = null },
  }
}

/* Text as the server takes it: no NUL or other control characters but tab and newline, CRLF as
 * LF, then cut into posts of at most MAX_TEXT characters (never splitting a surrogate pair) whose
 * JSON stays under MAX_TEXT_BYTES. The size is counted as it goes (linear), and counts every
 * non-ASCII UTF-16 unit as 6 bytes (its \uXXXX escape): more than its UTF-8 size, so a post is
 * under the limit however the server measures it. */
export function cleanText(t) {
  return String(t || '').replace(/\r\n?/g, '\n').replace(/[\u0000-\u0008\u000b-\u001f\u007f]/g, '')
}
function charBytes(ch) {
  let n = 0
  for (let i = 0; i < ch.length; i++) {
    const c = ch.charCodeAt(i)
    n += c >= 0x80 ? 6 : (c === 0x22 || c === 0x5c || c < 0x20) ? 2 : 1
  }
  return n
}
const QUOTES = 2
export function textChunks(text) {
  const out = []
  let cur = '', bytes = QUOTES
  for (const ch of cleanText(text)) {
    const b = charBytes(ch)
    if (cur && (cur.length + ch.length > MAX_TEXT || bytes + b > MAX_TEXT_BYTES)) { out.push(cur); cur = ''; bytes = QUOTES }
    cur += ch
    bytes += b
  }
  if (cur) out.push(cur)
  return out
}

/* A paste: at most MAX_PASTE characters (never splitting a surrogate pair). -> {text, cut} */
export function capPaste(text, max = MAX_PASTE) {
  const t = String(text || '')
  if (t.length <= max) return { text: t, cut: false }
  let end = max
  const c = t.charCodeAt(end - 1)
  if (c >= 0xd800 && c <= 0xdbff) end -= 1
  return { text: t.slice(0, end), cut: true }
}

/* The outgoing input, in order. push() adds an event: a move replaces a move still waiting at
 * the end of the queue, a wheel adds its deltas to a wheel waiting there, and text joins text waiting at the end (up to a post's limit). flush()
 * sends what is waiting, one post at a time and in order, and is called once per animation
 * frame; a flush that finds one still sending does nothing (what came meanwhile waits for the
 * next). `send(event)` returns a promise; a failed post is reported to onError and the rest go
 * on. */
export function createInputQueue(send, onError = () => {}) {
  let q = []
  let busy = false
  const isMove = (ev) => ev && ev.kind === 'mouse' && ev.type === 'move'
  return {
    push(ev) {
      const last = q[q.length - 1]
      if (isMove(ev) && isMove(last)) { q[q.length - 1] = ev; return }
      if (ev.kind === 'wheel' && last && last.kind === 'wheel' && last.modifiers === ev.modifiers) {
        const c = (d) => Math.max(-5000, Math.min(5000, d))
        q[q.length - 1] = { ...ev, dx: c(last.dx + ev.dx), dy: c(last.dy + ev.dy) }
        return
      }
      if (ev.kind === 'text') {
        const parts = textChunks(ev.text)
        if (!parts.length) return
        if (last && last.kind === 'text') {
          const joined = textChunks(last.text + parts.join(''))
          q.pop(); joined.forEach(t => q.push({ kind: 'text', text: t }))
        } else parts.forEach(t => q.push({ kind: 'text', text: t }))
        return
      }
      q.push(ev)
    },
    pending: () => q.length,
    busy: () => busy,
    clear() { q = [] },
    async flush() {
      if (busy || !q.length) return
      busy = true
      const batch = q; q = []
      try {
        for (const ev of batch) {
          try { await send(ev) } catch (err) { onError(err, ev) }
        }
      } finally { busy = false }
    },
  }
}

/* "Open it live": a reader page whose text is nearly empty, or that says it needs scripts */
export const NEEDS_SCRIPTS = /enable javascript|requires javascript|javascript is (disabled|required)/i
export function pageText(markdown) {
  return String(markdown || '')
    .replace(/!\[[^\]]*\]\([^)]*\)/g, '')            // pictures
    .replace(/\[([^\]]*)\]\([^)]*\)/g, '$1')         // links: their words
    .replace(/[#>*_`|~-]+/g, ' ')
    .replace(/\s+/g, ' ').trim()
}
export function offerLive(markdown) {
  const t = pageText(markdown)
  return t.length < 200 || NEEDS_SCRIPTS.test(String(markdown || ''))
}

/* "Open it live" for a reader page that failed: the address that failed, kept at failure time,
 * when it is a web address and the site failed it (an error from the site, or no answer). Not
 * when the reader refused the address itself: 403 with "… is not a public address", "the URL
 * host is not plain", "the URL has no host" or "only http and https URLs can be fetched"
 * (primer/grounding/web.py WebRefused, which primer/browse/service.py answers with 403), 400
 * (not an address) or 503 (web search off). The live open would refuse those too. */
export const READER_REFUSED = /is not a public address|host is not plain|has no host|only http and https/i
export function liveFallback(u, err) {
  const a = String(u || '').trim()
  if (!liveable(a)) return null
  const status = err && err.status
  if (status === 400 || status === 403 || status === 503) return null
  if (READER_REFUSED.test(String((err && err.message) || ''))) return null
  return a
}

/* the addresses the window can act on: `cur` for Ask and Make a page (a page that is open, live
 * or in the reader; never a failed one), `live` for the Live button (that, or a failed page
 * that may be opened live) */
export function pageTargets({ live, page, failed }) {
  if (live) { const u = liveable(live.u) ? live.u : null; return { cur: u, live: u } }
  if (page) return { cur: page.u, live: page.u }
  return { cur: null, live: failed || null }
}

/* The ask's words as parts: strings, and {b} for the parts shown in bold (the control's label
 * and the value). React escapes every part: nothing here is ever HTML. */
/* text without invisible format characters (Unicode Cf: bidi overrides and isolates, zero-width
 * marks, the BOM) or control characters, so the words in an ask read as what they are */
export function plainText(s) {
  return String(s == null ? '' : s).replace(/\p{Cf}/gu, '').replace(/[\s\p{Cc}]+/gu, ' ').trim()
}

export function askParts(ask) {
  const a = ask || {}
  const label = plainText(a.label)
  const value = plainText(a.value)
  const kind = a.action && typeof a.action === 'object' ? a.action.action : a.action
  if (label && kind === 'press') return ['The model wants to press ', { b: label }]
  if (label && kind === 'type') return ['The model wants to type ', { b: `‘${value}’` }, ' into ', { b: label }]
  if (label && kind === 'choose') return ['The model wants to choose ', { b: value }, ' in ', { b: label }]
  return [plainText(a.text) || 'The model wants to act on this page']
}

/* a live Click through step's words */
export function liveStepAction(step) {
  const a = (step && step.action) || {}
  const l = a.label ? `“${a.label}”` : a.n ? `control ${a.n}` : 'a control'
  switch (a.action) {
    case 'follow': return `follow ${l}`
    case 'scroll': return `scroll ${a.dir === 'up' ? 'up' : 'down'}`
    case 'press': return `press ${l}`
    case 'type': return `type ‘${a.text || ''}’ into ${l}`
    case 'choose': return `choose ${a.option || ''} in ${l}`
    case 'answer': return 'answer'
    default: return ''
  }
}

/* the Setup centre's line, from /v1/status live */
export function liveStatusLine(live) {
  if (!live) return ''
  if (!live.available) return 'Live pages need Chrome or Edge'
  return `Live pages: ${live.label || 'found'}` + (live.enabled === false ? ' (off while web search is off)' : '')
}

/* Can this address be opened live? Only web addresses; the server checks again. */
export const liveable = (u) => typeof u === 'string' && /^https?:\/\//i.test(u)

/* the content box's size as the open route takes it: whole numbers from 200 to 4000 */
export const liveSize = (n) => Math.max(200, Math.min(4000, Math.round(Number(n) || 0)))

/* base64 -> bytes (for a frame's JPEG) */
export function b64Bytes(b64) {
  const s = atob(String(b64 || ''))
  const out = new Uint8Array(s.length)
  for (let i = 0; i < s.length; i++) out[i] = s.charCodeAt(i)
  return out
}
