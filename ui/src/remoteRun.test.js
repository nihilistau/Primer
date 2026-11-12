/* remote.js, the Reader's side: the visibility rule, watchFocus's dedupe, and kairosRun's paths
 * (each command answers done exactly once) */
import { afterEach, beforeEach, describe, expect, test, vi } from 'vitest'
import {
  REMOTE_POLL_MS, _resetReaders, createListener, kairosRun, refused, registerReader, remoteSwitched, setReaderPage, started,
  watchFocus, watchRemoteEnabled,
} from './remote.js'

const enc = new TextEncoder()
const streamResponse = (chunks) => new Response(new ReadableStream({
  start(c) { chunks.forEach(s => c.enqueue(enc.encode(s))) },
}), { status: 200 })
const flush = () => new Promise(r => setTimeout(r, 0))
const HELLO = 'event: hello\ndata: {"listener":"tab-1"}\n\n'

describe('a background tab does not take the focus', () => {
  test('the listener sends a page change only while visible, but always on `always`', async () => {
    let visible = false
    const focus = vi.fn(async () => ({}))
    const lis = createListener({ fetch: vi.fn(async () => streamResponse([HELLO])), focus, done: vi.fn(), visible: () => visible })
    lis.reportFocus('n1', 'Bodiam Castle')   // before hello: kept
    await flush(); await flush()
    expect(focus).not.toHaveBeenCalled()     // hidden at hello: no resend
    lis.reportFocus('n2', 'Trebuchet')
    expect(focus).not.toHaveBeenCalled()
    lis.reportFocus('n2', 'Trebuchet', { always: true })   // the tab gained focus (still hidden: says so)
    expect(focus).toHaveBeenCalledWith('tab-1', 'n2', 'Trebuchet', true)
    visible = true
    lis.reportFocus('n3', 'Kenilworth Castle')
    expect(focus).toHaveBeenLastCalledWith('tab-1', 'n3', 'Kenilworth Castle')
    lis.close()
  })

  test('the resend after hello happens when visible', async () => {
    const focus = vi.fn(async () => ({}))
    const lis = createListener({ fetch: vi.fn(async () => streamResponse([HELLO])), focus, done: vi.fn(), visible: () => true })
    lis.reportFocus('n1', 'Bodiam Castle')
    await flush(); await flush()
    expect(focus).toHaveBeenCalledWith('tab-1', 'n1', 'Bodiam Castle')
    lis.close()
  })
})

describe('the stream only while Kairos may drive Primer', () => {
  beforeEach(() => { vi.useFakeTimers() })
  afterEach(() => { vi.useRealTimers() })
  const STREAM = '/v1/tool/stream'
  const streams = (fetch) => fetch.mock.calls.filter(([url]) => url === STREAM)
  const openFetch = () => vi.fn(async (url, init) => streamResponse([HELLO]))

  test('off: no stream; on connects; off aborts it and nothing retries', async () => {
    const fetch = openFetch()
    const states = []
    const lis = createListener({ fetch, focus: vi.fn(async () => ({})), done: vi.fn(), enabled: false,
                                 onState: (s) => states.push(s) })
    await vi.advanceTimersByTimeAsync(60000)
    expect(streams(fetch)).toHaveLength(0)
    lis.setEnabled(true)
    await vi.advanceTimersByTimeAsync(0)
    expect(streams(fetch)).toHaveLength(1)
    lis.setEnabled(true)   // already connected: no second stream
    await vi.advanceTimersByTimeAsync(0)
    expect(streams(fetch)).toHaveLength(1)
    const { signal } = fetch.mock.calls[0][1]
    expect(signal.aborted).toBe(false)
    lis.setEnabled(false)
    expect(signal.aborted).toBe(true)
    expect(states.at(-1)).toEqual({ connected: false })
    await vi.advanceTimersByTimeAsync(60000)
    expect(streams(fetch)).toHaveLength(1)
    lis.close()
  })

  test('a 403 (the switch is off at the server) waits for the status, never retrying on its own', async () => {
    let n = 0
    const fetch = vi.fn(async () => (++n === 1 ? new Response('{}', { status: 403 }) : streamResponse([HELLO])))
    const lis = createListener({ fetch, focus: vi.fn(async () => ({})), done: vi.fn() })
    await vi.advanceTimersByTimeAsync(60000)
    expect(fetch).toHaveBeenCalledTimes(1)
    lis.setEnabled(true)   // the status said on
    await vi.advanceTimersByTimeAsync(0)
    expect(fetch).toHaveBeenCalledTimes(2)
    lis.close()
  })

  test('watchRemoteEnabled follows /v1/status, and this tab\'s own switch at once', async () => {
    let on = false
    const status = vi.fn(async () => ({ remote: { enabled: on, listening: false } }))
    const fetch = openFetch()
    const lis = createListener({ fetch, focus: vi.fn(async () => ({})), done: vi.fn(), enabled: false })
    const off = watchRemoteEnabled({ listener: lis, status })
    await vi.advanceTimersByTimeAsync(REMOTE_POLL_MS * 3)
    expect(status.mock.calls.length).toBeGreaterThanOrEqual(3)
    expect(streams(fetch)).toHaveLength(0)        // disabled: no stream, only the status poll
    on = true
    await vi.advanceTimersByTimeAsync(REMOTE_POLL_MS)
    expect(streams(fetch)).toHaveLength(1)        // the switch went on: connects
    await vi.advanceTimersByTimeAsync(REMOTE_POLL_MS * 2)
    expect(streams(fetch)).toHaveLength(1)        // still on: the same stream
    const { signal } = fetch.mock.calls[0][1]
    on = false
    remoteSwitched(false)                         // this tab's Engines window: at once
    expect(signal.aborted).toBe(true)
    remoteSwitched(true)
    await vi.advanceTimersByTimeAsync(0)
    expect(streams(fetch)).toHaveLength(2)
    off()
    remoteSwitched(false)                         // unwatched: no longer follows
    expect(fetch.mock.calls[1][1].signal.aborted).toBe(false)
    const calls = status.mock.calls.length
    await vi.advanceTimersByTimeAsync(REMOTE_POLL_MS * 3)
    expect(status.mock.calls.length).toBe(calls)
    lis.close()
  })
})

describe('watchFocus', () => {
  afterEach(() => { _resetReaders() })
  const setup = (visible = () => true) => {
    const wins = [{ id: 'reader:1', z: 1 }, { id: 'reader:2', z: 2 }]
    let onWindows = () => {}
    const win = new EventTarget()
    const listener = { reportFocus: vi.fn() }
    registerReader('reader:1', vi.fn())
    registerReader('reader:2', vi.fn())
    const off = watchFocus({ listener, getWindows: () => wins,
                             subscribeWindows: (f) => { onWindows = f; return () => {} }, win, visible })
    return { wins, listener, win, off, windowsChanged: () => onWindows() }
  }

  test('reports a page change once (deduped), and again on every tab focus', () => {
    const { listener, win, off, windowsChanged } = setup()
    setReaderPage('reader:2', 'n2', 'Trebuchet')
    expect(listener.reportFocus).toHaveBeenCalledWith('n2', 'Trebuchet', { always: false })
    windowsChanged(); windowsChanged()                // a window moved: the same page, nothing new
    setReaderPage('reader:1', 'n1', 'Bodiam Castle')  // not the top Reader: the focus is unchanged
    expect(listener.reportFocus).toHaveBeenCalledTimes(1)
    win.dispatchEvent(new Event('focus'))
    win.dispatchEvent(new Event('focus'))
    expect(listener.reportFocus).toHaveBeenCalledTimes(3)
    expect(listener.reportFocus).toHaveBeenLastCalledWith('n2', 'Trebuchet', { always: true })
    off()
    win.dispatchEvent(new Event('focus'))
    expect(listener.reportFocus).toHaveBeenCalledTimes(3)
  })

  test('the top Reader changing reports its page', () => {
    const { wins, listener, windowsChanged } = setup()
    setReaderPage('reader:1', 'n1', 'Bodiam Castle')
    setReaderPage('reader:2', 'n2', 'Trebuchet')
    wins[0].z = 5   // reader:1 came to the front
    windowsChanged()
    expect(listener.reportFocus).toHaveBeenLastCalledWith('n1', 'Bodiam Castle', { always: false })
  })

  test('a hidden tab skips page changes, but its focus event still reports', () => {
    let visible = false
    const { listener, win } = setup(() => visible)
    setReaderPage('reader:2', 'n2', 'Trebuchet')
    expect(listener.reportFocus).not.toHaveBeenCalled()
    win.dispatchEvent(new Event('focus'))
    expect(listener.reportFocus).toHaveBeenCalledWith('n2', 'Trebuchet', { always: true })
    visible = true
    setReaderPage('reader:2', 'n3', 'Kenilworth Castle')
    expect(listener.reportFocus).toHaveBeenLastCalledWith('n3', 'Kenilworth Castle', { always: false })
  })
})

describe('kairosRun', () => {
  const ST = { image_available: { ok: true }, map_available: { ok: false, reason: 'no map tiles' },
               timeline_available: { ok: true }, diagram_available: { ok: true }, voice_available: { ok: true } }
  /* a fake Reader: run() plays `script` through the hooks and after */
  const reader = ({ script = ['node'], cached = false, streaming = false, lensErr = '', imageErr = '',
                    route = { kind: 'page', body: { concept: 'Bodiam Castle', via: 'typed' } },
                    status = async () => ST } = {}) => {
    const calls = { run: [], lens: [] }
    const run = vi.fn(async (body, mode, after, hooks) => {
      calls.run.push(body)
      const id = body.node_id || 'new1'
      const title = body.node_id === 'n2' ? 'Trebuchet' : 'Bodiam Castle'
      for (const step of script) {
        if (step === 'node') hooks && hooks.started(id, title, cached)
        else if (step === 'error') hooks && hooks.failed('The model stopped')
        else if (step === 'abort') hooks && hooks.failed('Another page was opened first')
        else if (step === 'written') after && after(id, title)
      }
    })
    const carryOut = vi.fn(async (r, hooks) => {
      if (r.kind === 'image' || r.kind === 'video') return imageErr
      return run(r.body, 'push', undefined, hooks)
    })
    const lens = vi.fn(async (kind, id, title) => { calls.lens.push([kind, id, title]); return lensErr })
    const deps = { nodeId: 'n1', title: 'Bodiam Castle', streaming, routePrompt: vi.fn(async () => route),
                   carryOut, run, lens, status }
    return { calls, deps }
  }
  const go = async (cmd, opts) => {
    const r = reader(opts)
    const finish = vi.fn()
    await kairosRun(cmd, r.deps, finish)
    return { finish, ...r }
  }
  const show = { kind: 'show', args: { text: 'show me Bodiam Castle' } }

  test('show: done once at the node event, via kairos on the page request', async () => {
    const { finish, calls, deps } = await go(show, { script: ['node', 'written'] })
    expect(deps.routePrompt).toHaveBeenCalledWith('show me Bodiam Castle', { nodeId: 'n1' })
    expect(calls.run[0].via).toBe('kairos')
    expect(finish).toHaveBeenCalledTimes(1)
    expect(finish).toHaveBeenCalledWith(started('new1', 'Bodiam Castle', 'writing'))
  })

  test('show of a page already read answers opened', async () => {
    const { finish } = await go(show, { cached: true })
    expect(finish).toHaveBeenCalledWith(started('new1', 'Bodiam Castle', 'opened'))
  })

  test('an error after the node event: still done once, as started', async () => {
    const { finish } = await go(show, { script: ['node', 'error'] })
    expect(finish).toHaveBeenCalledTimes(1)
    expect(finish.mock.calls[0][0].ok).toBe(true)
  })

  test('an error before any node fails it', async () => {
    const { finish } = await go(show, { script: ['error'] })
    expect(finish).toHaveBeenCalledTimes(1)
    expect(finish).toHaveBeenCalledWith(refused('The model stopped'))
  })

  test('abort fails it', async () => {
    const { finish } = await go({ kind: 'open', args: { id: 'n2' } }, { script: ['abort'] })
    expect(finish).toHaveBeenCalledTimes(1)
    expect(finish).toHaveBeenCalledWith(refused('Another page was opened first'))
  })

  test('a stream that ends with no node event still answers (failed), once', async () => {
    const { finish } = await go(show, { script: [] })
    expect(finish).toHaveBeenCalledTimes(1)
    expect(finish.mock.calls[0][0].ok).toBe(false)
  })

  test('show of a picture answers when the job starts, or with its refusal', async () => {
    const route = { kind: 'image', body: { node_id: 'n1', subject: 'a moat' } }
    let r = await go(show, { route })
    expect(r.finish).toHaveBeenCalledWith(started('n1', 'a moat', 'picture'))
    expect(r.calls.run).toEqual([])
    r = await go(show, { route, imageErr: 'Images are not available right now.' })
    expect(r.finish).toHaveBeenCalledWith(refused('Images are not available right now.'))
  })

  test('open: by id, answers opened', async () => {
    const { finish, calls } = await go({ kind: 'open', args: { id: 'n2', title: 'Trebuchet' } })
    expect(calls.run).toEqual([{ node_id: 'n2' }])
    expect(finish).toHaveBeenCalledTimes(1)
    expect(finish).toHaveBeenCalledWith(started('n2', 'Trebuchet', 'opened'))
  })

  test('a lens on the page shown presses the button handler', async () => {
    const { finish, calls } = await go({ kind: 'lens', args: { kind: 'timeline', id: 'n1' } })
    expect(calls.run).toEqual([])
    expect(calls.lens).toEqual([['timeline', 'n1', 'Bodiam Castle']])
    expect(finish).toHaveBeenCalledWith(started('n1', 'Bodiam Castle', 'timeline'))
  })

  test('a lens on another page opens it first, then presses, once it is written', async () => {
    const { finish, calls } = await go({ kind: 'lens', args: { kind: 'diagram', id: 'n2' } }, { script: ['node', 'written'] })
    expect(calls.run).toEqual([{ node_id: 'n2' }])
    expect(calls.lens).toEqual([['diagram', 'n2', 'Trebuchet']])
    expect(finish).toHaveBeenCalledTimes(1)
    expect(finish).toHaveBeenCalledWith(started('n2', 'Trebuchet', 'diagram'))
  })

  test('a lens on another page that never finished opening fails', async () => {
    const { finish, calls } = await go({ kind: 'lens', args: { kind: 'diagram', id: 'n2' } }, { script: ['node'] })
    expect(calls.lens).toEqual([])
    expect(finish).toHaveBeenCalledTimes(1)
    expect(finish).toHaveBeenCalledWith(refused('Primer could not open that page'))
  })

  test('a lens on the page still being written fails', async () => {
    const { finish, calls } = await go({ kind: 'lens', args: { kind: 'picture', id: 'n1' } }, { streaming: true })
    expect(calls.lens).toEqual([])
    expect(finish).toHaveBeenCalledWith(refused('Primer is still writing this page'))
  })

  test('a disabled lens answers with the button reason; a refused job with its error', async () => {
    let r = await go({ kind: 'lens', args: { kind: 'map', id: 'n1' } })
    expect(r.finish).toHaveBeenCalledWith(refused('no map tiles'))
    expect(r.calls.lens).toEqual([])
    r = await go({ kind: 'lens', args: { kind: 'story', id: 'n1' } }, { lensErr: 'That is already starting' })
    expect(r.finish).toHaveBeenCalledWith(refused('That is already starting'))
    r = await go({ kind: 'lens', args: { kind: 'listen', id: 'n1' } }, { status: async () => { throw new Error('down') } })
    expect(r.finish).toHaveBeenCalledWith(refused('Narration is not available right now.'))
  })

  test('a bad command or a throwing route answers failed, once', async () => {
    const r = await go({ kind: 'nope', args: {} })
    expect(r.finish).toHaveBeenCalledWith(refused('Primer does not know that command'))
    const finish = vi.fn()
    const { deps } = reader()
    deps.routePrompt = async () => { throw new Error('intent down') }
    await kairosRun(show, deps, finish)
    expect(finish).toHaveBeenCalledTimes(1)
    expect(finish).toHaveBeenCalledWith(refused('intent down'))
  })
})

describe('the tab that ran Kairos\'s command keeps the focus while hidden', () => {
  // a stream that stays open after its events, as the server's does
  const openStream = (events) => new Response(new ReadableStream({
    start(c) { const enc = new TextEncoder(); for (const e of events) c.enqueue(enc.encode(e)) },
  }))
  const CMD = 'event: command\ndata: {"id":"c1","kind":"show","args":{"text":"Trebuchet"}}\n\n'

  test('a hidden tab reports its page once it has run a command; a hidden tab that has not stays quiet', async () => {
    const focus = vi.fn(async () => ({}))
    const quiet = createListener({ fetch: vi.fn(async () => openStream([HELLO])), focus, done: vi.fn(),
                                   visible: () => false })
    await flush(); await flush()
    expect(quiet.owns()).toBe(false)
    quiet.reportFocus('n9', 'Somewhere else')
    expect(focus).not.toHaveBeenCalled()
    quiet.close()

    const ran = createListener({ fetch: vi.fn(async () => openStream([HELLO, CMD])), focus, done: vi.fn(),
                                 onCommand: () => {}, visible: () => false })
    await flush(); await flush()
    expect(ran.owns()).toBe(true)
    ran.reportFocus('n2', 'Trebuchet')
    // hidden, so the server keeps the page but won't make this the focused tab
    expect(focus).toHaveBeenCalledWith('tab-1', 'n2', 'Trebuchet', true)
    ran.close()
  })

  test('watchFocus lets an owning listener report while hidden', () => {
    _resetReaders()
    const wins = [{ id: 'reader:1', z: 1 }]
    const listener = { reportFocus: vi.fn(), owns: () => true }
    registerReader('reader:1', vi.fn())
    const off = watchFocus({ listener, getWindows: () => wins, subscribeWindows: () => () => {},
                             win: new EventTarget(), visible: () => false })
    setReaderPage('reader:1', 'n2', 'Trebuchet')
    expect(listener.reportFocus).toHaveBeenCalledWith('n2', 'Trebuchet', { always: false })
    off(); _resetReaders()
  })
})
