/* testDom — a small DOM for mounting windows in tests (vitest runs in node, and the plan adds no
 * dependencies, so there is no jsdom). Only what React DOM 18, BoundBook and the Timeline and
 * Diagram windows touch: nodes and the tree, attributes, style, dataset, innerHTML (parsed with
 * sanitize.js's own reader), a small selector engine (tag, .class, [attr], [attr=value],
 * descendants, lists), events with capture and bubbling, focus with focusin/focusout, and hooks
 * for the sizes layout reads. Test-only: nothing in the app imports it. */
import { HTML_NS, parseFragment } from '../bound/sanitize.js'

const SVG_NS = 'http://www.w3.org/2000/svg'
const VOID = new Set(['br', 'img', 'hr', 'wbr', 'input', 'meta', 'link', 'source', 'col', 'area', 'base', 'embed', 'param', 'track'])
const esc = (s) => String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
const escAttr = (s) => esc(s).replace(/"/g, '&quot;')

export class TestEvent {
  constructor(type, init = {}) {
    Object.assign(this, init)
    this.type = type
    this.bubbles = init.bubbles !== undefined ? !!init.bubbles : true
    this.cancelable = true
    this.defaultPrevented = false
    this.timeStamp = Date.now()
    this.isTrusted = true
    this._stop = false
    this._stopNow = false
  }
  preventDefault() { this.defaultPrevented = true }
  stopPropagation() { this._stop = true }
  stopImmediatePropagation() { this._stop = true; this._stopNow = true }
}

class TNode {
  constructor(doc) {
    this.ownerDocument = doc
    this.parentNode = null
    this.childNodes = []
    this._ls = []
  }
  get firstChild() { return this.childNodes[0] || null }
  get lastChild() { return this.childNodes[this.childNodes.length - 1] || null }
  get parentElement() { return this.parentNode && this.parentNode.nodeType === 1 ? this.parentNode : null }
  _sib(d) { const p = this.parentNode; if (!p) return null; return p.childNodes[p.childNodes.indexOf(this) + d] || null }
  get nextSibling() { return this._sib(1) }
  get previousSibling() { return this._sib(-1) }
  get isConnected() { let n = this; while (n.parentNode) n = n.parentNode; return n.nodeType === 9 }
  appendChild(c) { return this.insertBefore(c, null) }
  replaceChildren(...nodes) {
    for (const c of [...this.childNodes]) this.removeChild(c)
    for (const n of nodes) this.appendChild(typeof n === 'string' ? this.ownerDocument.createTextNode(n) : n)
  }
  insertBefore(c, ref) {
    if (c.nodeType === 11) { for (const k of [...c.childNodes]) this.insertBefore(k, ref); return c }
    if (c.parentNode) c.parentNode.removeChild(c)
    const at = ref ? this.childNodes.indexOf(ref) : -1
    if (at < 0) this.childNodes.push(c); else this.childNodes.splice(at, 0, c)
    c.parentNode = this
    mutated(this)
    return c
  }
  removeChild(c) {
    const at = this.childNodes.indexOf(c)
    if (at >= 0) this.childNodes.splice(at, 1)
    c.parentNode = null
    const doc = this.ownerDocument || this
    if (doc._active && c.contains(doc._active)) doc._active = null
    mutated(this)
    return c
  }
  contains(n) { for (; n; n = n.parentNode) if (n === this) return true; return false }
  get textContent() { return this.childNodes.map((c) => c.textContent).join('') }
  set textContent(v) {
    for (const c of [...this.childNodes]) this.removeChild(c)
    if (v !== '' && v != null) this.appendChild(this.ownerDocument.createTextNode(String(v)))
  }
  addEventListener(type, fn, opts) {
    if (!fn) return
    const capture = typeof opts === 'boolean' ? opts : !!(opts && opts.capture)
    if (!this._ls.some((l) => l.type === type && l.fn === fn && l.capture === capture)) this._ls.push({ type, fn, capture })
  }
  removeEventListener(type, fn, opts) {
    const capture = typeof opts === 'boolean' ? opts : !!(opts && opts.capture)
    this._ls = this._ls.filter((l) => !(l.type === type && l.fn === fn && l.capture === capture))
  }
  dispatchEvent(ev) {
    const path = []
    for (let n = this; n; n = n.parentNode || (n.nodeType === 9 ? n.defaultView : null)) path.push(n)
    ev.target = this
    const run = (node, phase, capture) => {
      ev.currentTarget = node
      ev.eventPhase = phase
      for (const l of [...node._ls]) {
        if (l.type !== ev.type || (phase !== 2 && l.capture !== capture)) continue
        l.fn.call(node, ev)
        if (ev._stopNow) return
      }
    }
    const prev = globalThis.event
    globalThis.event = ev
    try {
      for (let k = path.length - 1; k > 0 && !ev._stop; k--) run(path[k], 1, true)
      if (!ev._stop) run(this, 2, null)
      if (ev.bubbles) for (let k = 1; k < path.length && !ev._stop; k++) run(path[k], 3, false)
    } finally { globalThis.event = prev }
    return !ev.defaultPrevented
  }
}

const mutated = (n) => { const d = n && (n.nodeType === 9 ? n : n.ownerDocument); if (d && d._hooks && d._hooks.mutate) d._hooks.mutate() }

class TText extends TNode {
  constructor(doc, v, type = 3) { super(doc); this.nodeType = type; this._v = String(v) }
  get nodeValue() { return this._v }
  set nodeValue(v) { this._v = String(v); mutated(this) }
  get nodeName() { return this.nodeType === 8 ? '#comment' : '#text' }
  get data() { return this.nodeValue }
  set data(v) { this.nodeValue = String(v) }
  get textContent() { return this.nodeType === 8 ? '' : this.nodeValue }
  set textContent(v) { this.nodeValue = String(v) }
}

function makeStyle() {
  const props = {}
  return new Proxy(props, {
    get(t, k) {
      if (k === 'setProperty') return (n, v) => { t[n] = String(v) }
      if (k === 'removeProperty') return (n) => { delete t[n] }
      if (k === 'getPropertyValue') return (n) => t[n] ?? ''
      return t[k] ?? ''
    },
    set(t, k, v) { t[k] = v === null || v === undefined ? '' : String(v); return true },
  })
}

/* ------------------------------------------------------------------ selectors */

function parseCompound(s) {
  const out = { tag: null, classes: [], attrs: [] }
  const re = /([A-Za-z][\w-]*)|\.([\w-]+)|\[([\w-]+)(?:=(?:"([^"]*)"|'([^']*)'|([^\]]*)))?\]/g
  let m
  while ((m = re.exec(s))) {
    if (m[1]) out.tag = m[1].toLowerCase()
    else if (m[2]) out.classes.push(m[2])
    else out.attrs.push({ name: m[3].toLowerCase(), value: m[4] ?? m[5] ?? m[6] })
  }
  return out
}
function matchCompound(el, c) {
  if (el.nodeType !== 1) return false
  if (c.tag && el.localName.toLowerCase() !== c.tag) return false
  const cls = (el.getAttribute('class') || '').split(/\s+/)
  if (c.classes.some((k) => !cls.includes(k))) return false
  for (const a of c.attrs) {
    if (!el.hasAttribute(a.name)) return false
    if (a.value !== undefined && el.getAttribute(a.name) !== a.value) return false
  }
  return true
}
function matchOne(el, sel) {
  const parts = (sel.trim().match(/(?:[^\s[]+|\[[^\]]*\])+/g) || []).map(parseCompound) // spaces inside [...] kept
  if (!matchCompound(el, parts[parts.length - 1])) return false
  let k = parts.length - 2
  for (let n = el.parentNode; n && k >= 0; n = n.parentNode) if (n.nodeType === 1 && matchCompound(n, parts[k])) k--
  return k < 0
}
const matchList = (el, sel) => sel.split(',').some((s) => matchOne(el, s))

/* ------------------------------------------------------------------ elements */

class TElement extends TNode {
  constructor(doc, localName, ns = HTML_NS) {
    super(doc)
    this.nodeType = 1
    this.localName = ns === HTML_NS ? localName.toLowerCase() : localName
    this.namespaceURI = ns
    this._attrs = new Map()
    this.style = makeStyle()
    this.dataset = {} // not reflected to data-* attributes
  }
  get tagName() { return this.namespaceURI === HTML_NS ? this.localName.toUpperCase() : this.localName }
  get nodeName() { return this.tagName }
  _key(n) { return this.namespaceURI === HTML_NS ? String(n).toLowerCase() : String(n) }
  setAttribute(n, v) {
    this._attrs.set(this._key(n), String(v))
    // as in Chrome, a focused control that becomes disabled loses focus (to the body)
    if (this._key(n) === 'disabled' && this.ownerDocument._active === this) this.ownerDocument._focus(null)
  }
  setAttributeNS(_ns, n, v) { this.setAttribute(n, v) }
  getAttribute(n) { const v = this._attrs.get(this._key(n)); return v === undefined ? null : v }
  hasAttribute(n) { return this._attrs.has(this._key(n)) }
  removeAttribute(n) { this._attrs.delete(this._key(n)) }
  removeAttributeNS(_ns, n) { this.removeAttribute(n) }
  get attributes() { return [...this._attrs].map(([name, value]) => ({ name, value })) }
  get id() { return this.getAttribute('id') || '' }
  get className() { return this.getAttribute('class') || '' }
  set className(v) { this.setAttribute('class', v) }
  get classList() { const el = this; return { contains: (c) => el.className.split(/\s+/).includes(c) } }
  get disabled() { return this.hasAttribute('disabled') }
  get tabIndex() { const v = this.getAttribute('tabindex'); return v === null ? -1 : Number(v) }
  get children() { return this.childNodes.filter((c) => c.nodeType === 1) }
  matches(sel) { return matchList(this, sel) }
  closest(sel) { for (let n = this; n && n.nodeType === 1; n = n.parentNode) if (matchList(n, sel)) return n; return null }
  querySelectorAll(sel) {
    const out = []
    const walk = (n) => { for (const c of n.childNodes) if (c.nodeType === 1) { if (matchList(c, sel)) out.push(c); walk(c) } }
    walk(this)
    return out
  }
  querySelector(sel) { return this.querySelectorAll(sel)[0] || null }
  get innerHTML() { return this.childNodes.map(serialize).join('') }
  set innerHTML(html) {
    for (const c of [...this.childNodes]) this.removeChild(c)
    for (const n of parseFragment(html).childNodes) this.appendChild(build(this.ownerDocument, n))
  }
  get outerHTML() { return serialize(this) }
  focus() {
    // a disabled control can't take focus (React's commit tries to give focus back to one)
    if (this.hasAttribute('disabled') && /^(button|input|select|textarea)$/.test(this.localName)) return
    this.ownerDocument._focus(this)
  }
  blur() { if (this.ownerDocument._active === this) this.ownerDocument._focus(null) }
  click() { this.dispatchEvent(new TestEvent('click', { button: 0, detail: 1 })) }
  getBoundingClientRect() { return this.ownerDocument._hooks.rect(this) }
  get clientWidth() { return this.ownerDocument._hooks.size(this).w }
  get clientHeight() { return this.ownerDocument._hooks.size(this).h }
  setPointerCapture() {}
  releasePointerCapture() {}
  hasPointerCapture() { return false }
  scrollIntoView() {}
}

function build(doc, n) {
  if (n.nodeType === 3 || n.nodeType === 4) return doc.createTextNode(n.nodeValue)
  const el = doc.createElementNS(n.namespaceURI || HTML_NS, n.localName)
  for (const a of n.attributes || []) el.setAttribute(a.name, a.value)
  for (const c of n.childNodes || []) el.appendChild(build(doc, c))
  return el
}

function serialize(n) {
  if (n.nodeType === 3) return esc(n.nodeValue)
  if (n.nodeType === 8) return `<!--${n.nodeValue}-->`
  const attrs = n.attributes.map((a) => ` ${a.name}="${escAttr(a.value)}"`).join('')
  if (n.namespaceURI === HTML_NS && VOID.has(n.localName)) return `<${n.localName}${attrs}>`
  return `<${n.localName}${attrs}>${n.childNodes.map(serialize).join('')}</${n.localName}>`
}

class TDocument extends TNode {
  constructor(win, hooks) {
    super(null)
    this.nodeType = 9
    this.defaultView = win
    this._hooks = hooks
    this._active = null
    this.documentElement = this.createElement('html')
    this.head = this.createElement('head')
    this.body = this.createElement('body')
    this.documentElement.appendChild(this.head)
    this.documentElement.appendChild(this.body)
    this.appendChild(this.documentElement)
  }
  get nodeName() { return '#document' }
  get activeElement() { return this._active || this.body }
  createElement(tag) { return new TElement(this, String(tag), HTML_NS) }
  createElementNS(ns, tag) { return new TElement(this, String(tag), ns || HTML_NS) }
  createTextNode(v) { return new TText(this, v) }
  createComment(v) { return new TText(this, v, 8) }
  createDocumentFragment() { const f = new TElement(this, '#fragment'); f.nodeType = 11; return f }
  querySelector(sel) { return this.documentElement.querySelector(sel) }
  querySelectorAll(sel) { return this.documentElement.querySelectorAll(sel) }
  getElementById() { return null }
  hasFocus() { return true }
  _focus(el) {
    const old = this._active
    if (old === el) return
    this._active = el
    if (old) { old.dispatchEvent(new TestEvent('blur', { bubbles: false })); old.dispatchEvent(new TestEvent('focusout', { relatedTarget: el })) }
    if (el) { el.dispatchEvent(new TestEvent('focus', { bubbles: false })); el.dispatchEvent(new TestEvent('focusin', { relatedTarget: old })) }
  }
}

/* ------------------------------------------------------------------ install */

const KEYS = ['window', 'document', 'navigator', 'localStorage', 'requestAnimationFrame', 'cancelAnimationFrame',
  'matchMedia', 'ResizeObserver', 'HTMLIFrameElement', 'HTMLElement', 'Node', 'getSelection', 'addEventListener',
  'removeEventListener', 'dispatchEvent', 'CustomEvent', 'event', 'IS_REACT_ACT_ENVIRONMENT']

/* Install the DOM on globalThis. hooks: rect(el) -> {left, top, right, bottom, width, height} and
 * size(el) -> {w, h}; mutate() is called after any change to the tree or to a text. Returns {document, observers, uninstall, selection}. */
export function installDom(hooks = {}) {
  const saved = KEYS.map((k) => [k, Object.getOwnPropertyDescriptor(globalThis, k)])
  const h = {
    rect: hooks.rect || (() => ({ left: 0, top: 0, right: 0, bottom: 0, width: 0, height: 0 })),
    size: hooks.size || (() => ({ w: 0, h: 0 })),
    mutate: hooks.mutate || null,
  }
  const win = new TNode(null)
  const document = new TDocument(win, h)
  const observers = []
  const store = new Map()
  const selection = { text: '' }
  const define = (k, v) => Object.defineProperty(globalThis, k, { value: v, configurable: true, writable: true, enumerable: true })
  define('window', globalThis)
  define('document', document)
  define('localStorage', {
    getItem: (k) => (store.has(k) ? store.get(k) : null), setItem: (k, v) => { store.set(k, String(v)) },
    removeItem: (k) => { store.delete(k) }, clear: () => store.clear(),
  })
  define('requestAnimationFrame', (cb) => setTimeout(() => cb(performance.now()), 16))
  define('cancelAnimationFrame', (id) => clearTimeout(id))
  define('matchMedia', () => ({ matches: false, addEventListener() {}, removeEventListener() {} }))
  define('ResizeObserver', class {
    constructor(cb) { this.cb = cb; observers.push(this) }
    observe() {}
    disconnect() { const k = observers.indexOf(this); if (k >= 0) observers.splice(k, 1) }
  })
  define('HTMLIFrameElement', class {})
  define('HTMLElement', TElement)
  define('Node', TNode)
  define('getSelection', () => ({ toString: () => selection.text, rangeCount: 0, removeAllRanges() {} }))
  define('addEventListener', (...a) => win.addEventListener(...a))
  define('removeEventListener', (...a) => win.removeEventListener(...a))
  define('dispatchEvent', (ev) => win.dispatchEvent(ev))
  define('CustomEvent', class extends TestEvent { constructor(type, init = {}) { super(type, init); this.detail = init.detail } })
  define('event', undefined)
  return {
    document, observers, selection,
    uninstall() {
      for (const [k, d] of saved) {
        if (d) Object.defineProperty(globalThis, k, d)
        else delete globalThis[k]
      }
    },
  }
}

export { SVG_NS }
