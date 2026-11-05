import { afterEach, describe, expect, it, vi } from 'vitest'
import * as api from './api.js'

const ok = () => ({ ok: true, json: async () => ({}) })

describe('api', () => {
  afterEach(() => { vi.unstubAllGlobals() })

  it('sends Content-Type: application/json on every POST (the server refuses others with 415)', async () => {
    const fetch = vi.fn(async () => ok())
    vi.stubGlobal('fetch', fetch)
    await api.serverAction('text', 'stop')
    await api.setTier('light')
    for (const [, init] of fetch.mock.calls) {
      expect(init.method).toBe('POST')
      expect(init.headers['Content-Type']).toBe('application/json')
    }
  })
})

const sseBody = (chunks, onCancel) => {
  const enc = new TextEncoder()
  return new ReadableStream({
    start(c) { chunks.forEach(s => c.enqueue(enc.encode(s))) /* left open: a stalled server */ },
    cancel: onCancel,
  })
}

describe('openPage', () => {
  afterEach(() => { vi.unstubAllGlobals() })

  it('streams events and releases the body when the caller throws mid-stream', async () => {
    let cancelled = false
    vi.stubGlobal('fetch', vi.fn(async () => new Response(
      sseBody(['event: node\ndata: {"id":"n"}\n\nevent: delta\ndata: {"text":"a"}\n\n'], () => { cancelled = true }),
      { status: 200 })))
    const seen = []
    const onEvent = (e) => { seen.push(e.event); if (e.event === 'delta') throw new Error('boom') }
    await expect(api.openPage({ concept: 'x', via: 'start' }, onEvent)).rejects.toThrow('boom')
    expect(seen).toEqual(['node', 'delta'])
    expect(cancelled).toBe(true)
  })

  it('delivers every event of a stream that closes', async () => {
    const enc = new TextEncoder()
    const body = new ReadableStream({ start(c) {
      c.enqueue(enc.encode('event: delta\ndata: {"text":"é'))
      c.enqueue(enc.encode('"}\n\nevent: done\ndata: {"n":1}\n\n'))
      c.close()
    } })
    vi.stubGlobal('fetch', vi.fn(async () => new Response(body, { status: 200 })))
    const seen = []
    await api.openPage({ concept: 'x', via: 'start' }, (e) => seen.push(e))
    expect(seen).toEqual([{ event: 'delta', data: { text: 'é' } }, { event: 'done', data: { n: 1 } }])
  })
})

describe('X-Primer header', () => {
  afterEach(() => { vi.unstubAllGlobals() })

  it('is sent on every request, GET and POST and page streams', async () => {
    const fetch = vi.fn(async () => new Response(new ReadableStream({ start(c) { c.close() } }), { status: 200 }))
    vi.stubGlobal('fetch', fetch)
    await api.status().catch(() => {})
    await api.branches('n', 'p').catch(() => {})
    await api.serverAction('text', 'stop').catch(() => {})
    await api.openPage({ concept: 'x', via: 'start' }, () => {})
    expect(fetch).toHaveBeenCalledTimes(4)
    for (const [, init] of fetch.mock.calls) expect(init.headers['X-Primer']).toBe('1')
  })
})

describe('image calls', () => {
  afterEach(() => { vi.unstubAllGlobals() })

  const run = async (fn) => {
    const fetch = vi.fn(async () => ok())
    vi.stubGlobal('fetch', fetch)
    await fn()
    return fetch.mock.calls[0]
  }

  it('POSTs send X-Primer and a JSON body', async () => {
    const cases = [
      [() => api.intent('draw a cat'), '/v1/intent', { text: 'draw a cat' }],
      [() => api.createImage({ node_id: 'n', subject: 's' }), '/v1/images', { node_id: 'n', subject: 's' }],
      [() => api.cancelImage('a b'), '/v1/images/a%20b/cancel', {}],
      [() => api.saveImageModel({ id: 'm' }), '/v1/image-models', { model: { id: 'm' } }],
      [() => api.deleteImageModel('m'), '/v1/image-models/delete', { id: 'm' }],
      [() => api.setDefaultImageModel('m'), '/v1/image-models/default', { id: 'm' }],
      [() => api.scanFolder('/models'), '/v1/image-models/scan', { folder: '/models' }],
    ]
    for (const [fn, url, body] of cases) {
      const [u, init] = await run(fn)
      expect(u).toBe(url)
      expect(init.method).toBe('POST')
      expect(init.headers).toMatchObject({ 'X-Primer': '1', 'Content-Type': 'application/json' })
      expect(JSON.parse(init.body)).toEqual(body)
    }
  })

  it('GETs send X-Primer', async () => {
    for (const [fn, url] of [[() => api.imageJob('a/b'), '/v1/images/a%2Fb'], [() => api.imageModels(), '/v1/image-models']]) {
      const [u, init] = await run(fn)
      expect(u).toBe(url)
      expect(init.method).toBe('GET')
      expect(init.headers['X-Primer']).toBe('1')
    }
  })

  it('mediaUrl accepts only <sha256>.png names', () => {
    const ok64 = 'a'.repeat(64) + '.png'
    expect(api.mediaUrl(ok64)).toBe('/v1/media/' + ok64)
    for (const bad of ['..', 'a/b', 'x?y', 'abc.png', '', null, undefined, 'A'.repeat(64) + '.png']) {
      expect(api.mediaUrl(bad)).toBe('')
    }
  })

  it('videoUrl accepts only <sha256>.webm names', () => {
    const ok64 = 'b'.repeat(64) + '.webm'
    expect(api.videoUrl(ok64)).toBe('/v1/media/' + ok64)
    for (const bad of ['b'.repeat(64) + '.png', 'x.webm', '../' + ok64, '', null]) expect(api.videoUrl(bad)).toBe('')
    expect(api.mediaUrl(ok64)).toBe('')
  })

  it('a failed request carries its HTTP status', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => ({ ok: false, status: 404, json: async () => ({ error: 'not found' }) })))
    await expect(api.imageJob('x')).rejects.toMatchObject({ message: 'not found', status: 404 })
  })
})

describe('setup calls', () => {
  afterEach(() => { vi.unstubAllGlobals() })

  const run = async (fn) => {
    const fetch = vi.fn(async () => ok())
    vi.stubGlobal('fetch', fetch)
    await fn()
    return fetch.mock.calls[0]
  }

  it('POSTs send X-Primer and a JSON body', async () => {
    const cases = [
      [() => api.setupPlan('light', { map: 'z9' }, '/data'), '/v1/setup/plan', { kit: 'light', choices: { map: 'z9' }, root: '/data' }],
      [() => api.setupPlan('light', undefined, '/data'), '/v1/setup/plan', { kit: 'light', choices: {}, root: '/data' }],
      [() => api.setupStart('light', '/data', { map: 'z9' }), '/v1/setup/start', { kit: 'light', root: '/data', choices: { map: 'z9' } }],
      [() => api.setupStart('light', '/data'), '/v1/setup/start', { kit: 'light', root: '/data', choices: {} }],
      [() => api.setupPause(), '/v1/setup/pause', {}],
      [() => api.setupResume(), '/v1/setup/resume', {}],
      [() => api.setupCancel('zim_top'), '/v1/setup/cancel', { item: 'zim_top' }],
      [() => api.setupLicence('other <https://l/x>'), '/v1/setup/licence', { licence: 'other <https://l/x>' }],
      [() => api.setupApprove('/x/a.exe', 'ab'), '/v1/setup/approve', { path: '/x/a.exe', sha256: 'ab' }],
      [() => api.setupApproveRelease('llama_cpp'), '/v1/setup/approve-release', { item: 'llama_cpp' }],
      [() => api.setupMove(['/a.gguf'], '/d'), '/v1/setup/move', { paths: ['/a.gguf'], dest: '/d' }],
      [() => api.setupMoveCancel(), '/v1/setup/move/cancel', {}],
    ]
    for (const [fn, url, body] of cases) {
      const [u, init] = await run(fn)
      expect(u).toBe(url)
      expect(init.method).toBe('POST')
      expect(init.headers).toMatchObject({ 'X-Primer': '1', 'Content-Type': 'application/json' })
      expect(JSON.parse(init.body)).toEqual(body)
    }
  })

  it('GETs send X-Primer (updates needs it)', async () => {
    const cases = [[() => api.setup(), '/v1/setup'], [() => api.setupModels(), '/v1/setup/models'],
                   [() => api.setupUpdates(), '/v1/setup/updates'], [() => api.setupUpdates(true), '/v1/setup/updates?refresh=1']]
    for (const [fn, url] of cases) {
      const [u, init] = await run(fn)
      expect(u).toBe(url)
      expect(init.method).toBe('GET')
      expect(init.headers['X-Primer']).toBe('1')
    }
  })
})

describe('reader, quiz and quest calls', () => {
  afterEach(() => { vi.unstubAllGlobals() })

  const run = async (fn) => {
    const fetch = vi.fn(async () => ok())
    vi.stubGlobal('fetch', fetch)
    await fn()
    return fetch.mock.calls[0]
  }

  it('POSTs send X-Primer and a JSON body', async () => {
    const cases = [
      [() => api.saveReader({ notes: 'I am 12' }), '/v1/reader', { notes: 'I am 12' }],
      [() => api.saveReader({ enabled: false }), '/v1/reader', { enabled: false }],
      [() => api.resetReader(), '/v1/reader/reset', {}],
      [() => api.refreshReader(), '/v1/reader/refresh', {}],
      [() => api.readerDwell('n1', 30), '/v1/reader/dwell', { node_id: 'n1', seconds: 30 }],
      [() => api.quizMake('n1'), '/v1/quiz/make', { node_id: 'n1', fresh: false }],
      [() => api.quizMake('n1', true), '/v1/quiz/make', { node_id: 'n1', fresh: true }],
      [() => api.quizAnswer('i1', 'an answer'), '/v1/quiz/answer', { item_id: 'i1', answer: 'an answer' }],
      [() => api.quizAnswer('i1', 'an answer', true), '/v1/quiz/answer', { item_id: 'i1', answer: 'an answer', revealed: true }],
      [() => api.questAbandon('q1'), '/v1/quest/abandon', { quest_id: 'q1' }],
      [() => api.questBuild(), '/v1/quest', {}],
      [() => api.questBuild('n1'), '/v1/quest', { from_node_id: 'n1' }],
      [() => api.questAnswer('q1', 'yes'), '/v1/quest/answer', { quest_id: 'q1', answer: 'yes' }],
      [() => api.questSkip('q1'), '/v1/quest/skip', { quest_id: 'q1' }],
    ]
    for (const [fn, url, body] of cases) {
      const [u, init] = await run(fn)
      expect(u).toBe(url)
      expect(init.method).toBe('POST')
      expect(init.headers).toMatchObject({ 'X-Primer': '1', 'Content-Type': 'application/json' })
      expect(JSON.parse(init.body)).toEqual(body)
    }
  })

  it('GETs send X-Primer and encode their query', async () => {
    const cases = [
      [() => api.readerState(), '/v1/reader'],
      [() => api.quizFor('a b'), '/v1/quiz?node_id=a%20b'],
      [() => api.quizDue(), '/v1/quiz/due?limit=20'],
      [() => api.quizDue(1), '/v1/quiz/due?limit=1'],
      [() => api.quizItem('x&y'), '/v1/quiz/item?id=x%26y'],
      [() => api.quest(), '/v1/quest'],
    ]
    for (const [fn, url] of cases) {
      const [u, init] = await run(fn)
      expect(u).toBe(url)
      expect(init.method).toBe('GET')
      expect(init.headers['X-Primer']).toBe('1')
    }
  })

  it('a dwell report is sent keepalive (it may go as the tab closes)', async () => {
    const [, init] = await run(() => api.readerDwell('n', 5))
    expect(init.keepalive).toBe(true)
    const [, other] = await run(() => api.saveReader({ notes: 'x' }))
    expect(other.keepalive).toBeUndefined()
  })

  it('a 204 (dwell) resolves', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => new Response(null, { status: 204 })))
    await expect(api.readerDwell('n', 5)).resolves.toEqual({})
  })
})
