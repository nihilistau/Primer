import { afterEach, beforeEach, describe, expect, test, vi } from 'vitest'
import {
  BACKOFF_MAX_MS, BACKOFF_MIN_MS, NOTE_MS, _resetReaders, createListener, dispatchCommand, focusedPage, kairosRoute,
  kairosRun, lensButton, lensCall, nextDelay, noteText, planCommand, refused, registerReader, setReaderPage, showWhat, started,
  subscribeReaders, topReader, watchFocus,
} from './remote.js'

const enc = new TextEncoder()
/* a stream response: `chunks` sent at once, then left open (closed when `end`) */
const streamResponse = (chunks, { end = false } = {}) => new Response(new ReadableStream({
  start(c) { chunks.forEach(s => c.enqueue(enc.encode(s))); if (end) c.close() },
}), { status: 200 })
const flush = () => new Promise(r => setTimeout(r, 0))

describe('createListener', () => {
  test('remembers its listener from hello, hands commands on with a finish that posts done once', async () => {
    const commands = []
    const done = vi.fn(async () => ({}))
    const focus = vi.fn(async () => ({}))
    const states = []
    const fetch = vi.fn(async () => streamResponse([
      'event: hello\ndata: {"listener":"tab-1"}\n\n',
      ': ping\n\n',
      'event: command\ndata: {"id":"c1","kind":"show","args":{"text":"show me Bodiam Castle"},"status":"delivered"}\n\n',
    ]))
    const lis = createListener({ fetch, done, focus, onState: (s) => states.push(s),
                                 onCommand: (cmd, finish) => { commands.push(cmd); finish(started('n1', 'Bodiam Castle', 'writing')); finish(refused('twice')) } })
    await flush(); await flush()
    expect(fetch).toHaveBeenCalledWith('/v1/tool/stream', expect.objectContaining({ headers: { 'X-Primer': '1' } }))
    expect(states).toEqual([{ connected: true, listener: 'tab-1' }])
    expect(commands.map(c => [c.id, c.kind, c.args.text])).toEqual([['c1', 'show', 'show me Bodiam Castle']])
    expect(done).toHaveBeenCalledTimes(1)
    expect(done).toHaveBeenCalledWith('c1', 'tab-1', { ok: true, result: { node_id: 'n1', title: 'Bodiam Castle', what: 'writing' } })
    lis.reportFocus('n1', 'Bodiam Castle')
    expect(focus).toHaveBeenCalledWith('tab-1', 'n1', 'Bodiam Castle')
    lis.reportFocus(null, '')   // no page: nothing to report
    expect(focus).toHaveBeenCalledTimes(1)
    lis.close()
  })

  test('a throwing or rejecting handler answers the command failed', async () => {
    const done = vi.fn(async () => ({}))
    const fetch = vi.fn(async () => streamResponse([
      'event: hello\ndata: {"listener":"tab-1"}\n\n',
      'event: command\ndata: {"id":"c1","kind":"open","args":{"id":"n1"}}\n\n',
      'event: command\ndata: {"id":"c2","kind":"open","args":{"id":"n2"}}\n\n',
    ]))
    const lis = createListener({ fetch, done, focus: async () => ({}),
                                 onCommand: (cmd) => { if (cmd.id === 'c1') throw new Error('no reader'); return Promise.reject(new Error('gone')) } })
    await flush(); await flush()
    expect(done).toHaveBeenCalledWith('c1', 'tab-1', { ok: false, error: 'no reader' })
    expect(done).toHaveBeenCalledWith('c2', 'tab-1', { ok: false, error: 'gone' })
    lis.close()
  })

  test('ignores a command before hello', async () => {
    const onCommand = vi.fn()
    const fetch = vi.fn(async () => streamResponse(['event: command\ndata: {"id":"c1","kind":"open","args":{"id":"n1"}}\n\n']))
    const lis = createListener({ fetch, onCommand, done: vi.fn(), focus: vi.fn() })
    await flush(); await flush()
    expect(onCommand).not.toHaveBeenCalled()
    lis.close()
  })

  describe('reconnecting', () => {
    beforeEach(() => { vi.useFakeTimers() })
    afterEach(() => { vi.useRealTimers() })

    test('backs off from 1 s, doubling to 10 s, while the stream fails', async () => {
      const fetch = vi.fn(async () => { throw new TypeError('Failed to fetch') })
      const states = []
      const lis = createListener({ fetch, onState: (s) => states.push(s), done: vi.fn(), focus: vi.fn() })
      await vi.advanceTimersByTimeAsync(0)
      expect(fetch).toHaveBeenCalledTimes(1)
      const waits = [1000, 2000, 4000, 8000, 10000, 10000]
      for (const [i, ms] of waits.entries()) {
        await vi.advanceTimersByTimeAsync(ms - 1)
        expect(fetch).toHaveBeenCalledTimes(i + 1)
        await vi.advanceTimersByTimeAsync(1)
        expect(fetch).toHaveBeenCalledTimes(i + 2)
      }
      expect(states.every(s => s.connected === false)).toBe(true)
      lis.close()
    })

    test('a failed stream (not 2xx) backs off too', async () => {
      const fetch = vi.fn(async () => new Response('{}', { status: 503 }))
      const lis = createListener({ fetch, done: vi.fn(), focus: vi.fn() })
      await vi.advanceTimersByTimeAsync(0)
      await vi.advanceTimersByTimeAsync(1000)
      expect(fetch).toHaveBeenCalledTimes(2)
      lis.close()
    })

    test('after a stream that said hello, the next try is 1 s later and the focus is sent again', async () => {
      let n = 0
      const focus = vi.fn(async () => ({}))
      const fetch = vi.fn(async () => {
        n += 1
        if (n === 1) throw new Error('down')
        if (n === 2) throw new Error('down')
        return streamResponse([`event: hello\ndata: {"listener":"tab-${n}"}\n\n`], { end: true })
      })
      const lis = createListener({ fetch, focus, done: vi.fn() })
      lis.reportFocus('n1', 'Bodiam Castle')   // not connected yet: kept for the hello
      expect(focus).not.toHaveBeenCalled()
      await vi.advanceTimersByTimeAsync(0)      // try 1 fails: wait 1 s
      await vi.advanceTimersByTimeAsync(1000)   // try 2 fails: wait 2 s
      await vi.advanceTimersByTimeAsync(2000)   // try 3: hello, then the stream ends
      expect(focus).toHaveBeenCalledWith('tab-3', 'n1', 'Bodiam Castle')
      await vi.advanceTimersByTimeAsync(999)
      expect(fetch).toHaveBeenCalledTimes(3)
      await vi.advanceTimersByTimeAsync(1)      // the backoff started over at 1 s
      expect(fetch).toHaveBeenCalledTimes(4)
      expect(focus).toHaveBeenCalledWith('tab-4', 'n1', 'Bodiam Castle')
      lis.close()
    })

    test('close stops the stream and every retry', async () => {
      const fetch = vi.fn(async () => { throw new Error('down') })
      const lis = createListener({ fetch, done: vi.fn(), focus: vi.fn() })
      await vi.advanceTimersByTimeAsync(0)
      lis.close()
      await vi.advanceTimersByTimeAsync(60000)
      expect(fetch).toHaveBeenCalledTimes(1)
    })

    test('close aborts an open stream', async () => {
      let signal = null
      const fetch = vi.fn(async (url, init) => { signal = init.signal; return streamResponse(['event: hello\ndata: {"listener":"t"}\n\n']) })
      const lis = createListener({ fetch, done: vi.fn(), focus: vi.fn() })
      await vi.advanceTimersByTimeAsync(0)
      lis.close()
      expect(signal.aborted).toBe(true)
      await vi.advanceTimersByTimeAsync(60000)
      expect(fetch).toHaveBeenCalledTimes(1)
    })
  })

  test('nextDelay', () => {
    expect([0, 1000, 2000, 4000, 8000, 10000].map(nextDelay)).toEqual([1000, 2000, 4000, 8000, 10000, 10000])
    expect([BACKOFF_MIN_MS, BACKOFF_MAX_MS, NOTE_MS]).toEqual([1000, 10000, 8000])
  })
})

describe('which Reader takes a command', () => {
  afterEach(() => { _resetReaders() })
  const windows = [
    { id: 'reader:1', appId: 'reader', z: 12 },
    { id: 'graph:1', appId: 'graph', z: 30 },
    { id: 'reader:2', appId: 'reader', z: 20 },
    { id: 'reader:3', appId: 'reader', z: 40, minimized: true },
    { id: 'reader:4', appId: 'reader', z: 41, minimizing: true },
  ]

  test('the registered Reader on top; a minimised or minimising one is skipped, as Desktop skips it', () => {
    const regs = new Map([['reader:1', {}], ['reader:2', {}], ['reader:3', {}], ['reader:4', {}]])
    expect(topReader(windows, regs)).toBe('reader:2')
    expect(topReader(windows, new Map([['reader:1', {}]]))).toBe('reader:1')
    expect(topReader(windows, new Map([['reader:3', {}], ['reader:4', {}]]))).toBe(null)
    expect(topReader(windows, new Map())).toBe(null)
  })

  test('its page is the focus; a Reader with no page reports none', () => {
    const seen = vi.fn()
    subscribeReaders(seen)
    registerReader('reader:1', vi.fn())
    registerReader('reader:2', vi.fn())
    registerReader('reader:3', vi.fn())
    setReaderPage('reader:3', 'n3', 'Trebuchet')   // minimised: not the focus
    expect(focusedPage(windows)).toBe(null)
    setReaderPage('reader:1', 'n1', 'Bodiam Castle')
    expect(focusedPage(windows)).toBe(null)   // reader:2 is on top, with no page
    setReaderPage('reader:2', 'n2', 'Kenilworth Castle')
    expect(focusedPage(windows)).toEqual({ nodeId: 'n2', title: 'Kenilworth Castle' })
    const calls = seen.mock.calls.length
    setReaderPage('reader:2', 'n2', 'Kenilworth Castle')   // unchanged: no news
    expect(seen.mock.calls.length).toBe(calls)
  })

  test('dispatch goes to the top Reader', () => {
    const one = vi.fn(), two = vi.fn(), openReader = vi.fn()
    registerReader('reader:1', one)
    registerReader('reader:2', two)
    const finish = vi.fn()
    expect(dispatchCommand({ id: 'c1' }, finish, { windows, openReader })).toBe('reader:2')
    expect(two).toHaveBeenCalledWith({ id: 'c1' }, finish)
    expect(one).not.toHaveBeenCalled()
    expect(openReader).not.toHaveBeenCalled()
  })

  test('with no Reader, one opens and takes the command when it registers', () => {
    const openReader = vi.fn()
    const finish = vi.fn()
    expect(dispatchCommand({ id: 'c1' }, finish, { windows: [], openReader })).toBe(null)
    expect(openReader).toHaveBeenCalledTimes(1)
    const handle = vi.fn()
    const off = registerReader('reader:3', handle)
    expect(handle).toHaveBeenCalledWith({ id: 'c1' }, finish)
    off()
    expect(topReader([{ id: 'reader:3', z: 1 }])).toBe(null)
  })

  test('a second command while a Reader is opening fails the first', () => {
    const first = vi.fn()
    dispatchCommand({ id: 'c1' }, first, { windows: [], openReader: () => {} })
    dispatchCommand({ id: 'c2' }, vi.fn(), { windows: [], openReader: () => {} })
    expect(first).toHaveBeenCalledWith({ ok: false, error: 'Primer was asked for something else first' })
  })

  test('a stale unregister (StrictMode remount) leaves the new registration', () => {
    const a = vi.fn(), b = vi.fn()
    const offA = registerReader('reader:1', a)
    registerReader('reader:1', b)
    offA()
    expect(topReader([{ id: 'reader:1', z: 1 }])).toBe('reader:1')
  })
})

describe('what a command means', () => {
  test('planCommand maps each kind to a Reader action', () => {
    expect(planCommand({ kind: 'show', args: { text: '  show me Bodiam Castle ' } }, 'n1'))
      .toEqual({ type: 'show', text: 'show me Bodiam Castle' })
    expect(planCommand({ kind: 'open', args: { id: 'n2' } }, 'n1')).toEqual({ type: 'open', nodeId: 'n2' })
    expect(planCommand({ kind: 'lens', args: { kind: 'map', id: 'n1' } }, 'n1'))
      .toEqual({ type: 'lens', lens: 'map', nodeId: 'n1', here: true })
    expect(planCommand({ kind: 'lens', args: { kind: 'listen', id: 'n2' } }, 'n1'))
      .toEqual({ type: 'lens', lens: 'listen', nodeId: 'n2', here: false })
    expect(planCommand({ kind: 'lens', args: { kind: 'story' } }, 'n1'))
      .toEqual({ type: 'lens', lens: 'story', nodeId: 'n1', here: true })
    expect(planCommand({ kind: 'lens', args: { kind: 'story' } }, null)).toEqual({ error: 'Open a page in Primer first' })
    expect(planCommand({ kind: 'lens', args: { kind: 'sculpture', id: 'n1' } }, 'n1')).toEqual({ error: 'Primer does not know that command' })
    expect(planCommand({ kind: 'show', args: { text: ' ' } }, 'n1')).toEqual({ error: 'Primer does not know that command' })
    expect(planCommand({ kind: 'delete', args: {} }, 'n1')).toEqual({ error: 'Primer does not know that command' })
    expect(planCommand(null, 'n1')).toEqual({ error: 'Primer does not know that command' })
  })

  test('the note', () => {
    expect(noteText({ kind: 'show', args: { text: 'show me Bodiam Castle' } })).toBe('show me Bodiam Castle')
    expect(noteText({ kind: 'open', args: { id: 'n1', title: 'Bodiam Castle' } })).toBe('open Bodiam Castle')
    const lens = (kind) => noteText({ kind: 'lens', args: { kind, id: 'n1', title: 'Bodiam Castle' } })
    expect(['picture', 'map', 'timeline', 'diagram', 'story', 'listen'].map(lens)).toEqual([
      'a picture of Bodiam Castle', 'a map of Bodiam Castle', 'a timeline of Bodiam Castle',
      'a diagram of Bodiam Castle', 'a story about Bodiam Castle', 'to read Bodiam Castle aloud'])
    // no title (an older server): the fallback words
    expect(noteText({ kind: 'open', args: { id: 'n1' } })).toBe('open a page you read before')
    expect(noteText({ kind: 'open', args: { id: 'n1', title: '  ' } })).toBe('open a page you read before')
    expect(noteText({ kind: 'lens', args: { kind: 'map' } })).toBe('a map of this page')
    expect(noteText({ kind: 'lens', args: { kind: 'listen' } })).toBe('to read this page aloud')
    expect(noteText({ kind: 'lens', args: { kind: 'story' } })).toBe('a story about this page')
  })

  test('kairosRoute swaps typed for kairos on the page request only', () => {
    const page = { kind: 'page', body: { concept: 'Bodiam Castle', via: 'typed', from_id: 'n1' } }
    expect(kairosRoute(page)).toEqual({ kind: 'page', body: { concept: 'Bodiam Castle', via: 'kairos', from_id: 'n1' } })
    expect(page.body.via).toBe('typed')   // not changed in place
    const map = { kind: 'map', body: { concept: 'Bodiam Castle', via: 'typed' } }
    expect(kairosRoute(map).body.via).toBe('kairos')
    const image = { kind: 'image', body: { node_id: 'n1', subject: 'a moat' } }
    expect(kairosRoute(image)).toBe(image)
  })

  test('showWhat names what Kairos says; a page already read was opened', () => {
    expect(['page', 'image', 'video', 'map', 'timeline', 'diagram', 'story'].map(k => showWhat(k)))
      .toEqual(['writing', 'picture', 'video', 'map', 'timeline', 'diagram', 'story'])
    expect(showWhat('page', true)).toBe('opened')
    expect(showWhat('page', false)).toBe('writing')
    expect(showWhat('map', true)).toBe('map')   // the map is still new
  })

  test('started and refused are the done outcomes', () => {
    expect(started('n1', 'Bodiam Castle', 'opened')).toEqual({ ok: true, result: { node_id: 'n1', title: 'Bodiam Castle', what: 'opened' } })
    expect(started(undefined, 'a moat', 'picture')).toEqual({ ok: true, result: { title: 'a moat', what: 'picture' } })
    expect(refused('Maps are not available right now.')).toEqual({ ok: false, error: 'Maps are not available right now.' })
    expect(refused('')).toEqual({ ok: false, error: 'Primer could not do that' })
  })

  test('lensButton is the header button state, with its reason', () => {
    const st = {
      image_available: { ok: true }, map_available: { ok: false, reason: 'no map tiles' },
      timeline_available: { ok: true }, diagram_available: { ok: false },
      voice_available: { ok: false, reason: 'no voice model' },
    }
    expect(lensButton('picture', st)).toEqual({ enabled: true, reason: '' })
    expect(lensButton('map', st)).toEqual({ enabled: false, reason: 'no map tiles' })
    expect(lensButton('timeline', st).enabled).toBe(true)
    expect(lensButton('diagram', st)).toEqual({ enabled: false, reason: 'Diagrams are not available right now.' })
    expect(lensButton('story', st)).toEqual({ enabled: false, reason: 'no voice model' })
    expect(lensButton('listen', st)).toEqual({ enabled: false, reason: 'no voice model' })
    expect(lensButton('map', null)).toEqual({ enabled: false, reason: 'Maps are not available right now.' })
  })

  test('lensCall names the header handler and its arguments', () => {
    expect(lensCall('picture', 'n1', 'Bodiam Castle')).toEqual(['image', { node_id: 'n1', subject: 'Bodiam Castle' }])
    expect(lensCall('map', 'n1', 'Bodiam Castle')).toEqual(['map', { node_id: 'n1', subject: 'Bodiam Castle' }])
    expect(lensCall('timeline', 'n1', 'Bodiam Castle')).toEqual(['timeline', 'n1', 'Bodiam Castle'])
    expect(lensCall('diagram', 'n1', 'Bodiam Castle')).toEqual(['diagram', { node_id: 'n1', subject: 'Bodiam Castle' }])
    expect(lensCall('story', 'n1', 'Bodiam Castle')).toEqual(['story', 'n1', 'story', 'Bodiam Castle'])
    expect(lensCall('listen', 'n1', 'Bodiam Castle')).toEqual(['story', 'n1', 'listen', 'Bodiam Castle'])
  })
})

describe('started', () => {
  test('a blank title becomes "this page", so the server never refuses the done', async () => {
    const { started } = await import('./remote.js')
    expect(started('n1', '', 'map').result.title).toBe('this page')
    expect(started('n1', '   ', 'map').result.title).toBe('this page')
    expect(started('n1', undefined, 'map').result.title).toBe('this page')
    expect(started('n1', 'Bodiam Castle', 'map').result).toEqual({ node_id: 'n1', title: 'Bodiam Castle', what: 'map' })
  })
})
