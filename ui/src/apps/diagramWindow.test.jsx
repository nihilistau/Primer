/* the Diagram window mounted (the small test DOM, ../test/testDom.js: no jsdom): the drawing is
 * rebuilt into the window (a use and a marker that point inside it kept, an outside one not),
 * a click on a label or a part opens its page in the context of the diagrammed page, Again
 * starts afresh and polls the new diagram, and closing the window stops its polling. */
import { afterAll, afterEach, beforeAll, describe, expect, test, vi } from 'vitest'
import { installDom, TestEvent } from '../test/testDom.js'

vi.mock('../api.js', async (orig) => ({ ...(await orig()), diagramJob: vi.fn(), diagramAgain: vi.fn() }))

// as the server writes it: ids prefixed dg-, references only within the drawing
const SVG = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1000 700">' +
  '<defs><marker id="dg-arrow"><path d="M0 0 L10 5 L0 10 z"/></marker><g id="dg-weight"><rect width="40" height="40"/></g></defs>' +
  '<line x1="100" y1="100" x2="400" y2="100" marker-end="url(#dg-arrow)"/>' +
  '<line x1="100" y1="200" x2="400" y2="200" marker-end="url(http://evil.example/m.svg#dg-arrow)"/>' +
  '<use href="#dg-weight" x="500" y="500"/><use href="http://evil.example/s.svg#dg-weight"/>' +
  '<text x="100" y="80">The arm</text><text x="500" y="480">Counterweight</text><text x="800" y="90">Sling</text></svg>'
const DONE = { id: 'd1', status: 'done', node_id: 'n1', phrase: '', svg: SVG, caption: 'How a trebuchet throws',
               labels: ['The arm', 'Counterweight', 'Sling'] }

/* DOMParser for the test DOM: sanitize.js's reader, as a parsed document */
let parseFragment
class TestParser {
  parseFromString(s) {
    const root = parseFragment(s).childNodes.find((n) => n.nodeType === 1) || null
    return { documentElement: root, getElementsByTagName: () => [] }
  }
}

let dom, React, createRoot, wm, api, DiagramWindow, OPEN_PLACE_EVENT
beforeAll(async () => {
  dom = installDom()
  globalThis.DOMParser = TestParser
  ;({ parseFragment } = await import('../bound/sanitize.js'))
  React = await import('react')
  ;({ createRoot } = await import('react-dom/client'))
  wm = await import('../windowManager.js')
  api = await import('../api.js')
  DiagramWindow = (await import('./DiagramWindow.jsx')).default
  ;({ OPEN_PLACE_EVENT } = await import('../openImage.js'))
})
afterAll(() => { delete globalThis.DOMParser; if (dom) dom.uninstall() })

const tick = (ms = 0) => new Promise((r) => setTimeout(r, ms))
async function waitFor(fn, timeout = 3000) {
  const end = Date.now() + timeout
  let last
  for (;;) {
    try { const v = fn(); if (v) return v } catch (e) { last = e }
    if (Date.now() > end) throw last || new Error('waitFor timed out')
    await tick(5)
  }
}

/* the desktop's part that matters here: every diagram window's body, as Desktop renders it */
function Desk() {
  const ws = React.useSyncExternalStore(wm.subscribe, wm.getWindows)
  return ws.filter((w) => w.appId === 'diagram').map((w) => <DiagramWindow key={w.id} {...w.props} windowId={w.id} />)
}

const mounted = []
async function mount(props = { diagramId: 'd1', subject: 'Trebuchet', nodeId: 'n1' }) {
  const id = wm.open('diagram', { props })
  const host = document.createElement('div')
  document.body.appendChild(host)
  const root = createRoot(host)
  mounted.push({ root, host })
  root.render(<Desk />)
  await waitFor(() => host.querySelector('.dg'))
  return { host, id }
}
const placed = () => {
  const got = []
  const on = (e) => { got.push(e.detail.body); e.detail.handled = true }
  window.addEventListener(OPEN_PLACE_EVENT, on)
  return { got, off: () => window.removeEventListener(OPEN_PLACE_EVENT, on) }
}
const labels = (h) => h.querySelectorAll('.dg-paper text.dg-label')
const buttonNamed = (h, name) => h.querySelectorAll('button').find((b) => b.textContent.trim() === name)
const about = (label) => ({ concept: label, via: 'context', from_id: 'n1', phrase: label })

afterEach(() => {
  for (const { root, host } of mounted.splice(0)) { root.unmount(); document.body.removeChild(host) }
  wm._reset()
  vi.mocked(api.diagramJob).mockReset()
  vi.mocked(api.diagramAgain).mockReset()
})

describe('the Diagram window', () => {
  test('draws the rebuilt drawing: a use and a marker inside it kept, outside ones not', async () => {
    api.diagramJob.mockResolvedValue(DONE)
    const { host } = await mount()
    await waitFor(() => labels(host).length === 3)
    const svg = host.querySelector('.dg-paper svg')
    expect(svg.getAttribute('class')).toBe('dg-svg')
    // ids carry this drawing's own prefix, so a reference can't reach another window's drawing
    const [use] = svg.querySelectorAll('use')
    expect(svg.querySelectorAll('use').length).toBe(1)
    expect(use.getAttribute('href')).toMatch(/^#u\d+-dg-weight$/)
    expect(svg.querySelectorAll('g').map((g) => g.getAttribute('id'))).toContain(use.getAttribute('href').slice(1))
    const ends = svg.querySelectorAll('line').map((l) => l.getAttribute('marker-end'))
    expect(ends[0]).toBe(`url(#${svg.querySelector('marker').getAttribute('id')})`)
    expect(ends[1]).toBeNull()
    expect(svg.innerHTML).not.toContain('evil')
    expect(host.querySelector('.dg-caption').textContent).toBe('How a trebuchet throws')
    expect(host.querySelector('.dg-status').textContent).toContain('3 labelled parts')
    expect(wm.getWindows()[0].title).toBe('Diagram: Trebuchet')
  })

  test('a click on a label in the drawing, or on a part, opens its page in context', async () => {
    api.diagramJob.mockResolvedValue(DONE)
    const { host } = await mount()
    await waitFor(() => labels(host).length === 3)
    const p = placed()
    try {
      const arm = labels(host).find((t) => t.textContent === 'The arm')
      expect(arm.getAttribute('role')).toBe('button')
      arm.dispatchEvent(new TestEvent('click', { button: 0 }))
      labels(host)[2].dispatchEvent(new TestEvent('keydown', { key: 'Enter' }))
      buttonNamed(host, 'Counterweight').dispatchEvent(new TestEvent('click', { button: 0 }))
      expect(p.got).toEqual([about('The arm'), about('Sling'), about('Counterweight')])
      // shift + click: a new reader instead
      arm.dispatchEvent(new TestEvent('click', { button: 0, shiftKey: true }))
      expect(p.got.length).toBe(3)
      expect(wm.getWindows().find((w) => w.appId === 'reader').props.start).toEqual(about('The arm'))
    } finally { p.off() }
  })

  test('Again draws afresh and polls the new diagram', async () => {
    api.diagramJob.mockImplementation(async (id) => (id === 'd1' ? DONE
      : { ...DONE, id: 'd2', svg: SVG.replace('Sling', 'Pivot'), labels: ['The arm', 'Counterweight', 'Pivot'] }))
    let release
    api.diagramAgain.mockImplementation(() => new Promise((r) => { release = () => r({ id: 'd2' }) }))
    const { host } = await mount()
    await waitFor(() => labels(host).length === 3)
    buttonNamed(host, 'Again').dispatchEvent(new TestEvent('click', { button: 0 }))
    await waitFor(() => buttonNamed(host, 'Again').disabled)
    expect(api.diagramAgain).toHaveBeenCalledWith('d1')
    release()
    await waitFor(() => api.diagramJob.mock.calls.some(([id]) => id === 'd2'))
    await waitFor(() => labels(host).some((t) => t.textContent === 'Pivot'))
    expect(labels(host).length).toBe(3)                       // the old drawing is gone
    expect(buttonNamed(host, 'Pivot')).toBeTruthy()
    expect(buttonNamed(host, 'Again').disabled).toBe(false)
  })

  test('closing the window removes it and stops its polling', async () => {
    api.diagramJob.mockResolvedValue({ id: 'd1', status: 'drawing', node_id: 'n1', labels: [] })
    const { host, id } = await mount()
    await waitFor(() => host.querySelector('.dg-status').textContent.includes('Drawing'))
    expect(buttonNamed(host, 'Again').disabled).toBe(true)
    wm.close(id)                                              // what the window's close button does
    await waitFor(() => !host.querySelector('.dg'))
    const calls = api.diagramJob.mock.calls.length
    await tick(1300)                                          // longer than the poll's 1 s interval
    expect(api.diagramJob.mock.calls.length).toBe(calls)
  })
})
