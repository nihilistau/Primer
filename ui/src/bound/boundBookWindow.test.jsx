/* the bound book mounted (testDom.js, a small DOM: the plan adds no dependencies, so no jsdom):
 * keys and turns, Home and End, reduced motion, the HTML and link checks before insert, the
 * toolbar's arrow keys, the exact count announced once it settles, a key queued while the book
 * is laid out again, double-click selection in the outer third, and clicks after a lost capture.
 * testDom parses innerHTML with sanitize.js's own parseFragment, so the HTML check test proves
 * cleanBlocks is on the load path; it can't catch a browser parsing markup differently (mXSS). */
import { afterAll, afterEach, beforeAll, describe, expect, test, vi } from 'vitest'
import { installDom, TestEvent } from '../test/testDom.js'

vi.mock('../api.js', async (orig) => ({ ...(await orig()), bookBlocks: vi.fn(), liveBlocks: vi.fn() }))
vi.mock('../openBrowser.js', () => ({ openBrowser: vi.fn() }))

const stage = { w: 1040, h: 708 }
const measured = []
let slow = 0 // ms spent in each measurement, so a layout runs in several chunks
let onMutate = null // called after every change to the DOM

const spin = (ms) => { const end = performance.now() + ms; while (performance.now() < end) { /* busy */ } }
const box = (w, h) => ({ left: 0, top: 0, right: w, bottom: h, width: w, height: h, x: 0, y: 0 })

let dom, createRoot, BoundBook, api, openBrowser, browseAddressFor
beforeAll(async () => {
  dom = installDom({
    rect(el) {
      if (el.closest('.bb-measure')) {
        measured.push(el.innerHTML)
        if (slow) spin(slow)
        const lines = Math.max(1, Math.ceil(el.textContent.length / 50))
        return box(388, lines * 24)
      }
      if (el.classList.contains('bb-book')) return box(parseFloat(el.style.width) || 0, parseFloat(el.style.height) || 0)
      return box(0, 0)
    },
    size: (el) => (el.classList.contains('bb-stage') ? stage : { w: 0, h: 0 }),
    mutate: () => { if (onMutate) onMutate() },
  })
  ;({ createRoot } = await import('react-dom/client'))
  BoundBook = (await import('./BoundBook.jsx')).default
  api = await import('../api.js')
  ;({ openBrowser } = await import('../openBrowser.js'))
  ;({ browseAddressFor } = await import('../apps/browserNav.js'))
})
afterAll(() => dom && dom.uninstall())

/* ------------------------------------------------------------------ the book */

const words = (c, k) => `Paragraph ${c}.${k} ` + 'stone wall and tower '.repeat(9)
function book(chapters = 3, paras = 12, extra = {}) {
  const out = [{ kind: 'cover', chapter: 0, html: '<section class="cover"><h1 id="cover">Castles</h1></section>' },
    { kind: 'contents', chapter: 0, html: '<ol>' + Array.from({ length: chapters }, (_, i) => `<li><a href="#c${i + 1}">Chapter ${i + 1}</a></li>`).join('') + '</ol>' }]
  for (let c = 1; c <= chapters; c++) {
    out.push({ kind: 'chapter-head', chapter: c, html: `<h1 id="c${c}">Chapter ${c}</h1>`, node_id: `n${c}` })
    for (let k = 1; k <= paras; k++) out.push({ kind: 'para', chapter: c, html: (extra[`${c}.${k}`]) || `<p>${words(c, k)}</p>` })
  }
  out.push({ kind: 'back', chapter: 0, html: '<p>Made with Primer</p>' })
  return out
}

/* ------------------------------------------------------------------ helpers */

const mounted = []
const tick = (ms = 0) => new Promise((r) => setTimeout(r, ms))
async function waitFor(fn, timeout = 4000) {
  const end = Date.now() + timeout
  let last
  for (;;) {
    try { const v = fn(); if (v) return v } catch (e) { last = e }
    if (Date.now() > end) throw last || new Error('waitFor timed out')
    await tick(5)
  }
}

/* every new text the live region shows, in order, from now on */
function recordLive(h) {
  const said = []
  let last = live(h)
  onMutate = () => { const el = h.querySelector('.bb-sr'); const t = el ? el.textContent : ''; if (t && t !== last) said.push(t); last = t }
  return said
}
const exact = (t) => / of \d+(,|$)/.test(t) && !/about/.test(t)

const label = (h) => h.querySelector('.bb-label').textContent
const live = (h) => h.querySelector('.bb-sr').textContent
const rootOf = (h) => h.querySelector('.bb')
const key = (el, k) => { const e = new TestEvent('keydown', { key: k }); el.dispatchEvent(e); return e }
const doneLabel = (h) => { const l = h.querySelector('.bb-label'); return l && / of \d+$/.test(l.textContent) && !/about/.test(l.textContent) }
const settled = (h) => !h.querySelector('.bb-leaf')
const counted = (h) => Number(/of (\d+)$/.exec(label(h))[1])
const pageEl = (h, side = 'right') => h.querySelector(`.bb-static .bb-${side}`)

async function mount({ blocks = book(), reduced = false, props = {} } = {}) {
  window.matchMedia = () => ({ matches: reduced, addEventListener() {}, removeEventListener() {} })
  api.bookBlocks.mockResolvedValue({ blocks, title: 'Castles' })
  const host = document.createElement('div')
  document.body.appendChild(host)
  const root = createRoot(host)
  mounted.push({ root, host })
  root.render(<BoundBook windowId="bound:1" bookId="b1" {...props} />)
  await waitFor(() => host.querySelector('.bb-book'))
  return host
}

afterEach(() => {
  for (const { root, host } of mounted.splice(0)) { root.unmount(); host.remove ? host.remove() : document.body.removeChild(host) }
  localStorage.clear()
  measured.length = 0
  slow = 0
  stage.w = 1040; stage.h = 708
  dom.selection.text = ''
  onMutate = null
  vi.mocked(openBrowser).mockClear()
})

/* ------------------------------------------------------------------ keys and turns */

describe('keys turn the book', () => {
  test('three presses of → turn three leaves, one each, and focus moves to the new page', async () => {
    const h = await mount()
    await waitFor(() => doneLabel(h))
    expect(label(h)).toMatch(/^Page 1 of \d+$/)
    const r = rootOf(h)
    key(r, 'ArrowRight'); key(r, 'ArrowRight'); key(r, 'ArrowRight')
    await waitFor(() => settled(h) && /^Pages 6–7 of/.test(label(h)))
    await tick(50)
    expect(label(h)).toMatch(/^Pages 6–7 of/)
    expect(document.activeElement.getAttribute('role')).toBe('region')
    expect(document.activeElement.getAttribute('aria-label')).toMatch(/^Page [67]$/)
    key(r, 'ArrowLeft')
    await waitFor(() => settled(h) && /^Pages 4–5 of/.test(label(h)))
  })
  test('End goes to the back cover and Home to the front, with no leaf', async () => {
    const h = await mount()
    await waitFor(() => doneLabel(h))
    const r = rootOf(h)
    key(r, 'End')
    await tick(0)
    const n = counted(h)
    expect(label(h)).toBe(`Page ${n} of ${n}`)
    expect(h.querySelector('.bb-leaf')).toBe(null)
    key(r, 'Home')
    await tick(0)
    expect(label(h)).toBe(`Page 1 of ${n}`)
    expect(h.querySelector('.bb-leaf')).toBe(null)
  })
})

describe('reduced motion', () => {
  test('spreads cross-fade: no leaf is drawn, and a page edge drag does not start', async () => {
    const h = await mount({ reduced: true })
    await waitFor(() => doneLabel(h))
    expect(rootOf(h).className).toContain('bb-reduced')
    let leaf = false
    const watch = setInterval(() => { if (h.querySelector('.bb-leaf')) leaf = true }, 1)
    key(rootOf(h), 'ArrowRight')
    await tick(0)
    expect(label(h)).toMatch(/^Pages 2–3 of/)
    expect(h.querySelector('.bb-static').className).toContain('bb-fade')
    const down = new TestEvent('pointerdown', { button: 0, clientX: 930, clientY: 300, pointerId: 1 })
    pageEl(h).dispatchEvent(down)
    pageEl(h).dispatchEvent(new TestEvent('pointermove', { clientX: 700, clientY: 300, pointerId: 1 }))
    await tick(40)
    clearInterval(watch)
    expect(down.defaultPrevented).toBe(false)
    expect(leaf).toBe(false)
  })
})

/* ------------------------------------------------------------------ the checks before insert */

describe('the HTML check before insert', () => {
  test('a hostile block never reaches the page or the measurer', async () => {
    const hostile = '<p>Fine words <img src="x" onerror="alert(1)"><script>alert(2)</script>'
      + '<iframe src="https://example.org/"></iframe><b onclick="alert(3)">bold</b> <a href="javascript:alert(4)">bad link</a></p>'
    const h = await mount({ blocks: book(1, 2, { '1.1': hostile }) })
    await waitFor(() => doneLabel(h))
    key(rootOf(h), 'ArrowRight')
    await waitFor(() => settled(h) && /^Pages 2–3/.test(label(h)))
    const html = h.innerHTML
    expect(html).toContain('Fine words')
    for (const bad of ['onerror', '<script', 'alert(', '<iframe', 'onclick', 'javascript:']) {
      expect(html).not.toContain(bad)
      for (const m of measured) expect(m).not.toContain(bad)
    }
    expect(measured.some((m) => m.includes('Fine words'))).toBe(true)
  })
})

describe('the link check before insert', () => {
  const linked = () => book(2, 2, {
    '1.1': '<p>See <a href="https://example.org/castles">the web page</a> and <a href="http://bad host/">a bad one</a>'
      + ' and <a href="data:text/html,x">data</a> and <a href="#c2">chapter two</a>.</p>',
  })
  test('only checked links are kept; a web link asks first, then opens Primer\'s Browser', async () => {
    const h = await mount({ blocks: linked() })
    await waitFor(() => doneLabel(h))
    key(rootOf(h), 'ArrowRight')
    await waitFor(() => settled(h) && /^Pages 2–3/.test(label(h)))
    const hrefs = h.querySelectorAll('.bb-static a').map((a) => a.getAttribute('href'))
    expect(hrefs).toContain('https://example.org/castles')
    expect(hrefs).toContain('#c2')
    expect(hrefs.some((x) => /^data:|bad host/.test(x || ''))).toBe(false)

    const web = h.querySelectorAll('.bb-static a').find((a) => a.getAttribute('href') === 'https://example.org/castles')
    const aux = new TestEvent('auxclick', { button: 1, clientX: 300 })
    web.dispatchEvent(aux)
    expect(aux.defaultPrevented).toBe(true)
    await tick(0)
    h.querySelector('.bb-ask') && h.querySelector('.bb-ask').querySelectorAll('button')[1].click() // Cancel
    await tick(0)
    expect(h.querySelector('.bb-ask')).toBe(null)
    const click = new TestEvent('click', { button: 0, detail: 1, clientX: 300 })
    web.dispatchEvent(click)
    expect(click.defaultPrevented).toBe(true)
    await tick(0)
    const ask = h.querySelector('.bb-ask')
    expect(ask.getAttribute('aria-label')).toBe('Open in the Browser?')
    expect(ask.querySelector('.bb-ask-url').textContent).toBe('https://example.org/castles')
    expect(openBrowser).not.toHaveBeenCalled()
    ask.querySelectorAll('button')[0].click() // Open
    await tick(0)
    expect(openBrowser).toHaveBeenCalledTimes(1)
    expect(openBrowser).toHaveBeenCalledWith(browseAddressFor('https://example.org/castles') || 'https://example.org/castles')
    expect(h.querySelector('.bb-ask')).toBe(null)
  })
  test('a contents entry turns to its chapter', async () => {
    const h = await mount({ blocks: linked() })
    await waitFor(() => doneLabel(h))
    key(rootOf(h), 'ArrowRight')
    await waitFor(() => settled(h) && /^Pages 2–3/.test(label(h)))
    const to2 = h.querySelectorAll('.bb-static a').find((a) => a.getAttribute('href') === '#c2')
    to2.dispatchEvent(new TestEvent('click', { button: 0, detail: 1, clientX: 300 }))
    await waitFor(() => settled(h) && h.querySelector('.bb-static').textContent.includes('Chapter 2'))
    expect(openBrowser).not.toHaveBeenCalled()
  })
})

/* ------------------------------------------------------------------ the toolbar */

describe('the toolbar', () => {
  const bar = (h) => h.querySelector('.bb-bar')
  const buttons = (h) => bar(h).querySelectorAll('button')
  const name = (b) => b.getAttribute('aria-label') || b.textContent
  test('a roving tabindex: one button is in the tab order, and the arrows move focus in the bar', async () => {
    const h = await mount()
    await waitFor(() => doneLabel(h))
    const tabbable = buttons(h).filter((b) => b.getAttribute('tabindex') === '0')
    expect(tabbable.length).toBe(1)
    expect(tabbable[0].disabled).toBe(false) // Previous is disabled on the cover, so it is the page label
    expect(buttons(h).filter((b) => b !== tabbable[0]).every((b) => b.getAttribute('tabindex') === '-1')).toBe(true)
    tabbable[0].focus()
    const before = label(h)
    const e = key(document.activeElement, 'ArrowRight')
    await tick(0)
    expect(e.defaultPrevented).toBe(true)
    expect(name(document.activeElement)).toBe('Next page')
    expect(document.activeElement.getAttribute('tabindex')).toBe('0')
    expect(label(h)).toBe(before) // the arrow moved focus; it did not turn the page
    key(document.activeElement, 'End')
    await tick(0)
    expect(name(document.activeElement)).toBe('Sound')
    key(document.activeElement, 'ArrowRight') // wraps round, past the disabled Previous
    await tick(0)
    expect(name(document.activeElement)).toMatch(/^Page 1 of/)
    key(document.activeElement, 'ArrowLeft')
    await tick(0)
    expect(name(document.activeElement)).toBe('Sound')
    key(document.activeElement, 'Home')
    await tick(0)
    expect(name(document.activeElement)).toMatch(/^Page 1 of/)
    for (const k of ['PageDown', 'End', 'Home', 'ArrowLeft']) key(document.activeElement, k)
    await tick(30)
    expect(label(h)).toBe(before) // page-turn keys work only on the book
  })
})

describe('the toolbar keeps focus', () => {
  const name = (b) => b.getAttribute('aria-label') || b.textContent
  test('when the focused Next or Previous becomes disabled, focus moves to the other one', async () => {
    const h = await mount({ reduced: true, blocks: book(1, 3) })
    await waitFor(() => doneLabel(h))
    const next = h.querySelector('.bb-bar [aria-label="Next page"]')
    next.focus()
    for (let k = 0; k < 20 && !next.disabled; k++) { next.click(); await tick(0) }
    expect(next.disabled).toBe(true)
    await tick(0)
    expect(name(document.activeElement)).toBe('Previous page')
    expect(document.activeElement.getAttribute('tabindex')).toBe('0')
    const prev = document.activeElement
    for (let k = 0; k < 20 && !prev.disabled; k++) { prev.click(); await tick(0) }
    expect(prev.disabled).toBe(true)
    await tick(0)
    expect(name(document.activeElement)).toBe('Next page')
  })
})

/* ------------------------------------------------------------------ the announcement */

describe('the announcement', () => {
  test('the exact count is announced once the layout settles', async () => {
    slow = 3
    const h = await mount({ reduced: true, blocks: book(4, 14) })
    await waitFor(() => /about/.test(label(h)))
    key(rootOf(h), 'ArrowRight')
    await waitFor(() => /about/.test(live(h)))
    expect(live(h)).toMatch(/^Pages? 2(–3)? of about \d+/) // page 3 may not be laid out yet
    const said = recordLive(h)
    await waitFor(() => doneLabel(h), 8000)
    await tick(20)
    expect(live(h).startsWith(label(h))).toBe(true)
    expect(live(h)).not.toMatch(/about/)
    // laid out again at a size with the same pages: the exact count is not said again
    stage.w = 1030
    dom.observers.forEach((o) => o.cb([]))
    await waitFor(() => /about/.test(label(h)), 2000)
    await waitFor(() => doneLabel(h), 8000)
    await tick(50)
    expect(said.filter(exact)).toEqual([live(h)])
  })
})

/* ------------------------------------------------------------------ re-layout */

describe('a landing that clamps to the spread on screen', () => {
  test('is still announced, and the landing resolves', async () => {
    slow = 2
    const h = await mount({ blocks: book(4, 14) })
    await waitFor(() => doneLabel(h), 8000)
    key(rootOf(h), 'End')
    await tick(0)
    const before = counted(h)
    expect(live(h)).toBe(`Page ${before} of ${before}`)
    // a taller window: fewer pages, so the back cover's new spread is where the old index clamps to
    stage.h = 900
    dom.observers.forEach((o) => o.cb([]))
    await waitFor(() => /about/.test(label(h)), 2000)
    await waitFor(() => doneLabel(h), 8000)
    await waitFor(() => settled(h))
    await tick(50)
    const n = counted(h)
    expect(n).toBeLessThan(before)
    expect(label(h)).toBe(`Page ${n} of ${n}`)
    expect(live(h)).toBe(`Page ${n} of ${n}`)
    // the landing resolved, so a later turn and its return are said as usual
    key(rootOf(h), 'ArrowLeft')
    await waitFor(() => settled(h) && live(h) !== `Page ${n} of ${n}`)
    expect(live(h).startsWith(label(h))).toBe(true)
  })
})

describe('a key during re-layout', () => {
  test('is queued and applied from where the reader lands, never undone', async () => {
    slow = 2
    const h = await mount({ blocks: book(4, 14) })
    await waitFor(() => doneLabel(h), 8000)
    key(rootOf(h), 'End')
    await tick(0)
    expect(label(h)).toMatch(/^Page (\d+) of \1$/)
    // a smaller window: laid out again, from the back cover, into more pages
    stage.h = 560
    dom.observers.forEach((o) => o.cb([]))
    await waitFor(() => /about/.test(label(h)), 2000)
    const said = recordLive(h)
    key(rootOf(h), 'ArrowLeft')
    await waitFor(() => doneLabel(h), 8000)
    await waitFor(() => settled(h))
    await tick(900) // any turn still to come has run
    await waitFor(() => settled(h))
    const n = counted(h)
    expect(label(h)).not.toBe(`Page ${n} of ${n}`)
    expect(label(h)).toMatch(new RegExp(`\\b${n - 1} of ${n}$`)) // the spread before the back cover
    // only the spread the reader lands on is said with the new count, never one passed through
    expect(said.filter(exact)).toEqual([live(h)])
  })
})

/* ------------------------------------------------------------------ pointer */

describe('double-click in the outer third', () => {
  const at = (h, type, detail, x = 900) => {
    const e = new TestEvent(type, { button: 0, detail, clientX: x, clientY: 300 })
    pageEl(h).dispatchEvent(e)
    return e
  }
  test('a second click within 400 ms of a turn selects a word and does not turn', async () => {
    const h = await mount({ reduced: true })
    await waitFor(() => doneLabel(h))
    key(rootOf(h), 'ArrowRight')
    await tick(0)
    expect(label(h)).toMatch(/^Pages 2–3/)
    at(h, 'mousedown', 1); at(h, 'click', 1)
    await tick(0)
    expect(label(h)).toMatch(/^Pages 4–5/)
    const down = at(h, 'mousedown', 2)
    expect(down.defaultPrevented).toBe(false) // the browser selects the word
    dom.selection.text = 'stone'
    at(h, 'click', 2)
    await tick(0)
    expect(label(h)).toMatch(/^Pages 4–5/)
  })
  test('with motion on, a quick second click in the drag band selects and does not turn', async () => {
    const h = await mount()
    await waitFor(() => doneLabel(h))
    key(rootOf(h), 'ArrowRight')
    await waitFor(() => settled(h) && /^Pages 2–3/.test(label(h)))
    // a prevented pointerdown suppresses the compatibility mousedown, so none is sent here
    const ptr = (type, extra = {}) => {
      const e = new TestEvent(type, { button: 0, clientX: 930, clientY: 300, pointerId: 1, ...extra })
      pageEl(h).dispatchEvent(e)
      return e
    }
    ptr('pointerdown'); ptr('pointerup'); ptr('click', { detail: 1 })
    await tick(0)
    const second = ptr('pointerdown')
    expect(second.defaultPrevented).toBe(false) // left to the browser, which selects the word
    ptr('pointerup'); ptr('click', { detail: 2 })
    await waitFor(() => settled(h))
    await tick(50)
    expect(label(h)).toMatch(/^Pages 4–5/) // one leaf, not two
  })
  test('with motion on, a triple-click in the drag band turns one leaf only', async () => {
    const h = await mount()
    await waitFor(() => doneLabel(h))
    key(rootOf(h), 'ArrowRight')
    await waitFor(() => settled(h) && /^Pages 2–3/.test(label(h)))
    const ptr = (type, extra = {}) => {
      const e = new TestEvent(type, { button: 0, clientX: 930, clientY: 300, pointerId: 1, ...extra })
      pageEl(h).dispatchEvent(e)
      return e
    }
    ptr('pointerdown'); ptr('pointerup'); ptr('click', { detail: 1 })
    await tick(0)
    ptr('pointerdown'); ptr('pointerup'); ptr('click', { detail: 2 })
    await tick(0)
    const third = ptr('pointerdown')
    expect(third.defaultPrevented).toBe(false) // still selection: the browser selects the paragraph
    ptr('pointerup'); ptr('click', { detail: 3 })
    await waitFor(() => settled(h))
    await tick(50)
    expect(label(h)).toMatch(/^Pages 4–5/)
  })
  test('a slower second click still turns again', async () => {
    const h = await mount({ reduced: true })
    await waitFor(() => doneLabel(h))
    at(h, 'mousedown', 1); at(h, 'click', 1)
    await tick(0)
    expect(label(h)).toMatch(/^Pages 2–3/)
    await tick(420)
    const down = at(h, 'mousedown', 2)
    expect(down.defaultPrevented).toBe(true)
    at(h, 'click', 2)
    await tick(0)
    expect(label(h)).toMatch(/^Pages 4–5/)
  })
})

describe('focus stays in the book', () => {
  const inBook = (h) => document.activeElement !== document.body && rootOf(h).contains(document.activeElement)
  test('a click turn remounts the focused page, and focus moves to the new page, not the body', async () => {
    const h = await mount()
    await waitFor(() => doneLabel(h))
    pageEl(h).focus() // what a click's mousedown does outside the drag band
    expect(document.activeElement.getAttribute('role')).toBe('region')
    pageEl(h).dispatchEvent(new TestEvent('click', { button: 0, detail: 1, clientX: 900, clientY: 300 }))
    await tick(0)
    expect(h.querySelector('.bb-leaf')).not.toBe(null) // mid-turn: the static right page is a new one
    expect(inBook(h)).toBe(true)
    await waitFor(() => settled(h) && /^Pages 2–3/.test(label(h)))
    await tick(20)
    expect(document.activeElement.getAttribute('role')).toBe('region')
    // and the page keys still work from there
    key(document.activeElement, 'ArrowRight')
    await waitFor(() => settled(h) && /^Pages 4–5/.test(label(h)))
    expect(inBook(h)).toBe(true)
  })
  test('with reduced motion a click turn re-draws every page, and focus stays in the book', async () => {
    const h = await mount({ reduced: true })
    await waitFor(() => doneLabel(h))
    pageEl(h).focus()
    pageEl(h).dispatchEvent(new TestEvent('click', { button: 0, detail: 1, clientX: 900, clientY: 300 }))
    await tick(0)
    expect(label(h)).toMatch(/^Pages 2–3/)
    expect(inBook(h)).toBe(true)
  })
  test('a press in the drag band focuses the book when focus was on the body', async () => {
    const h = await mount()
    await waitFor(() => doneLabel(h))
    key(rootOf(h), 'ArrowRight') // open the book: a closed cover has no drag band
    await waitFor(() => settled(h) && /^Pages 2–3/.test(label(h)))
    await tick(20)
    document.activeElement.blur()
    await tick(0)
    expect(document.activeElement).toBe(document.body)
    const down = new TestEvent('pointerdown', { button: 0, clientX: 930, clientY: 300, pointerId: 1 })
    pageEl(h).dispatchEvent(down)
    expect(down.defaultPrevented).toBe(true) // so the browser's own mousedown focus never comes
    expect(document.activeElement).toBe(rootOf(h))
    pageEl(h).dispatchEvent(new TestEvent('pointerup', { button: 0, clientX: 930, clientY: 300, pointerId: 1 }))
  })
  test('focus the reader moved out of the book is not pulled back by a later turn', async () => {
    const h = await mount({ reduced: true })
    await waitFor(() => doneLabel(h))
    pageEl(h).focus()
    document.activeElement.blur() // the reader clicked outside the book
    await tick(0)
    h.querySelector('.bb-bar [data-rove="next"]').click() // a turn that re-draws every page
    await tick(0)
    expect(label(h)).toMatch(/^Pages 2–3/)
    expect(document.activeElement).toBe(document.body)
  })
})

describe('after a lost pointer capture', () => {
  const ptr = (h, type, x, extra = {}) => {
    const e = new TestEvent(type, { button: 0, clientX: x, clientY: 300, pointerId: 7, ...extra })
    pageEl(h).dispatchEvent(e)
    return e
  }
  // open at Pages 2–3: the closed cover is not dragged
  const opened = async () => {
    const h = await mount()
    await waitFor(() => doneLabel(h))
    key(rootOf(h), 'ArrowRight')
    await waitFor(() => settled(h) && /^Pages 2–3/.test(label(h)))
    return h
  }
  const drag = async (h) => {
    ptr(h, 'pointerdown', 930); ptr(h, 'pointermove', 920); ptr(h, 'pointermove', 910)
    await waitFor(() => h.querySelector('.bb-leaf'))
  }
  test('a drag let go by a lost capture does not swallow the next click', async () => {
    const h = await opened()
    await drag(h)
    ptr(h, 'lostpointercapture', 910)
    await waitFor(() => settled(h))
    expect(label(h)).toMatch(/^Pages 2–3/) // it fell back
    // a click with no pointer down (a screen reader's, or a keyboard's) soon after
    pageEl(h).dispatchEvent(new TestEvent('click', { button: 0, detail: 0, clientX: 900, clientY: 300 }))
    await waitFor(() => settled(h) && /^Pages 4–5/.test(label(h)), 1500)
  })
  test('a lost capture with no drag swallows nothing', async () => {
    const h = await opened()
    ptr(h, 'pointerdown', 930); ptr(h, 'lostpointercapture', 930)
    ptr(h, 'click', 930, { detail: 1 })
    await waitFor(() => settled(h) && /^Pages 4–5/.test(label(h)), 1500)
  })
  test('a real drag still suppresses its own click', async () => {
    const h = await opened()
    await drag(h)
    ptr(h, 'pointerup', 910); ptr(h, 'lostpointercapture', 910); ptr(h, 'click', 910, { detail: 1 })
    await waitFor(() => settled(h))
    await tick(50)
    expect(label(h)).toMatch(/^Pages 2–3/)
  })
  test('a drag whose capture was lost still suppresses the click its pointerup makes', async () => {
    const h = await opened()
    await drag(h)
    ptr(h, 'lostpointercapture', 910); ptr(h, 'pointerup', 910); ptr(h, 'click', 910, { detail: 1 })
    await waitFor(() => settled(h))
    await tick(50)
    expect(label(h)).toMatch(/^Pages 2–3/)
  })
  test('a lost capture then a blur still suppresses the click the drag\'s pointerup makes', async () => {
    const h = await opened()
    await drag(h)
    ptr(h, 'lostpointercapture', 910)
    window.dispatchEvent(new TestEvent('blur', { bubbles: false }))
    ptr(h, 'pointerup', 910); ptr(h, 'click', 910, { detail: 1 })
    await waitFor(() => settled(h))
    await tick(50)
    expect(label(h)).toMatch(/^Pages 2–3/)
  })
  test('a window blur mid-drag lets the leaf go and swallows no later click', async () => {
    const h = await opened()
    await drag(h)
    window.dispatchEvent(new TestEvent('blur', { bubbles: false }))
    await waitFor(() => settled(h))
    expect(label(h)).toMatch(/^Pages 2–3/)
    pageEl(h).dispatchEvent(new TestEvent('click', { button: 0, detail: 0, clientX: 900, clientY: 300 }))
    await waitFor(() => settled(h) && /^Pages 4–5/.test(label(h)), 1500)
  })
})
