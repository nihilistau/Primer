/* the Timeline window mounted (the small test DOM, ../test/testDom.js: no jsdom):
 * events drawn as pins, a click on one opens its page in the context of the timelined page,
 * Again starts afresh and polls the new timeline, and closing the window stops its polling. */
import { afterAll, afterEach, beforeAll, describe, expect, test, vi } from 'vitest'
import { installDom, TestEvent } from '../test/testDom.js'

vi.mock('../api.js', async (orig) => ({ ...(await orig()), timelineJob: vi.fn(), timelineAgain: vi.fn(), allTimelines: vi.fn() }))

const ev = (label, year, more = {}) => ({ label, date_text: String(year), year, month: null, day: null, end_year: null,
                                          precision: 'year', approx: false, ...more })
const DONE = { id: 't1', status: 'done', node_id: 'n1', page_title: 'Harlech Castle',
               events: [ev('Harlech Castle begun', 1283), ev('Siege of Harlech', 1468)] }

let dom, React, createRoot, act, wm, api, TimelineWindow, OPEN_PLACE_EVENT
beforeAll(async () => {
  dom = installDom({ size: (el) => (el.classList.contains('tmw-stage') ? { w: 800, h: 400 } : { w: 0, h: 0 }) })
  globalThis.IS_REACT_ACT_ENVIRONMENT = false
  React = await import('react')
  ;({ createRoot } = await import('react-dom/client'))
  wm = await import('../windowManager.js')
  api = await import('../api.js')
  TimelineWindow = (await import('./TimelineWindow.jsx')).default
  ;({ OPEN_PLACE_EVENT } = await import('../openImage.js'))
})
afterAll(() => dom && dom.uninstall())

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

/* the desktop's part that matters here: every timeline window's body, as Desktop renders it */
function Desk() {
  const ws = React.useSyncExternalStore(wm.subscribe, wm.getWindows)
  return ws.filter((w) => w.appId === 'timeline').map((w) => <TimelineWindow key={w.id} {...w.props} windowId={w.id} />)
}

const mounted = []
async function mount(props = { timelineId: 't1', subject: 'Harlech Castle', nodeId: 'n1' }) {
  const id = wm.open('timeline', { props })
  const host = document.createElement('div')
  document.body.appendChild(host)
  const root = createRoot(host)
  mounted.push({ root, host })
  root.render(<Desk />)
  await waitFor(() => host.querySelector('.tmw'))
  return { host, id }
}
const placed = () => {
  const got = []
  const on = (e) => { got.push(e.detail.body); e.detail.handled = true }
  window.addEventListener(OPEN_PLACE_EVENT, on)
  return { got, off: () => window.removeEventListener(OPEN_PLACE_EVENT, on) }
}
const pins = (h) => h.querySelectorAll('g.tmw-ev')
const buttonNamed = (h, name) => h.querySelectorAll('button').find((b) => b.textContent === name)

afterEach(() => {
  for (const { root, host } of mounted.splice(0)) { root.unmount(); document.body.removeChild(host) }
  wm._reset()
  vi.mocked(api.timelineJob).mockReset()
  vi.mocked(api.timelineAgain).mockReset()
  vi.mocked(api.allTimelines).mockReset()
})

describe('the Timeline window', () => {
  test('draws the events, and a click on one opens its page in the context of the timelined page', async () => {
    api.timelineJob.mockResolvedValue(DONE)
    const { host } = await mount()
    await waitFor(() => pins(host).length === 2)
    expect(host.querySelector('.tmw-label').textContent).toContain('2 events')
    expect(wm.getWindows()[0].title).toBe('Timeline: Harlech Castle')
    const p = placed()
    try {
      const siege = pins(host).find((g) => g.textContent.includes('Siege of Harlech'))
      siege.dispatchEvent(new TestEvent('click', { button: 0 }))
      expect(p.got).toEqual([{ concept: 'Siege of Harlech', via: 'context', from_id: 'n1', phrase: 'Siege of Harlech' }])
      // Enter on a focused pin does the same; shift + click opens a new reader instead
      pins(host)[0].dispatchEvent(new TestEvent('keydown', { key: 'Enter' }))
      expect(p.got[1].concept).toBe('Harlech Castle begun')
      siege.dispatchEvent(new TestEvent('click', { button: 0, shiftKey: true }))
      expect(p.got.length).toBe(2)
      const reader = wm.getWindows().find((w) => w.appId === 'reader')
      expect(reader.props.start).toEqual({ concept: 'Siege of Harlech', via: 'context', from_id: 'n1', phrase: 'Siege of Harlech' })
    } finally { p.off() }
  })

  test('a millennium-only date outside the fitted view is a marker at the edge, until zoomed', async () => {
    api.timelineJob.mockResolvedValue({ ...DONE, events: [...DONE.events, ev('Bronze Age farms', -1500, { precision: 'millennium' })] })
    const { host } = await mount()
    await waitFor(() => pins(host).length === 3)
    const edge = host.querySelectorAll('g.tmw-edge')
    expect(edge.length).toBe(1)
    expect(edge[0].getAttribute('aria-label')).toBe('Bronze Age farms, 2nd millennium BCE, before this view. Open a page about it')
    expect(edge[0].querySelector('circle').getAttribute('cx')).toBe('0')
    // zoomed, the view is the reader's own: what lies outside it isn't drawn
    host.querySelectorAll('button').find((b) => b.getAttribute('aria-label') === 'Zoom in').dispatchEvent(new TestEvent('click', { button: 0 }))
    await waitFor(() => host.querySelectorAll('g.tmw-edge').length === 0)
    expect(pins(host).every((g) => !g.textContent.includes('Bronze Age farms'))).toBe(true)
  })

  test('Again starts the timeline afresh and polls the new one', async () => {
    api.timelineJob.mockImplementation(async (id) => (id === 't1' ? DONE
      : { id: 't2', status: 'done', node_id: 'n1', page_title: 'Harlech Castle', events: [ev('Harlech Castle finished', 1289)] }))
    let release
    api.timelineAgain.mockImplementation(() => new Promise((r) => { release = () => r({ id: 't2' }) }))
    const { host } = await mount()
    await waitFor(() => pins(host).length === 2)
    const again = buttonNamed(host, 'Again')
    expect(again.disabled).toBe(false)
    again.dispatchEvent(new TestEvent('click', { button: 0 }))
    await waitFor(() => buttonNamed(host, 'Again').disabled)   // busy while it asks
    expect(api.timelineAgain).toHaveBeenCalledWith('t1')
    release()
    await waitFor(() => api.timelineJob.mock.calls.some(([id]) => id === 't2'))
    await waitFor(() => pins(host).length === 1)
    expect(pins(host)[0].textContent).toContain('Harlech Castle finished')
    expect(buttonNamed(host, 'Again').disabled).toBe(false)
  })

  test('closing the window removes it and stops its polling', async () => {
    api.timelineJob.mockResolvedValue({ id: 't1', status: 'reading', node_id: 'n1', events: [] })
    const { host, id } = await mount()
    await waitFor(() => host.querySelector('.tmw-label').textContent.includes('Reading the dates'))
    expect(buttonNamed(host, 'Again').disabled).toBe(true)   // not until it has ended
    wm.close(id)                                             // what the window's close button does
    await waitFor(() => !host.querySelector('.tmw'))
    const calls = api.timelineJob.mock.calls.length
    await tick(1300)                                         // longer than the poll's 1 s interval
    expect(api.timelineJob.mock.calls.length).toBe(calls)
  })
})
