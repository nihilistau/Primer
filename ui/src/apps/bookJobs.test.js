import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createFollower, createPreviewer } from './bookJobs.js'

const flush = async (n = 5) => { for (let i = 0; i < n; i++) await Promise.resolve() }
const deferred = () => { let resolve, reject; const p = new Promise((a, b) => { resolve = a; reject = b }); return { p, resolve, reject } }
const err = (status, message = 'x') => Object.assign(new Error(message), { status })

beforeEach(() => { vi.useFakeTimers() })
afterEach(() => { vi.useRealTimers() })

describe('createPreviewer', () => {
  it('asks once, 400 ms after the last change', async () => {
    const fetchPreview = vi.fn(async () => ({ chapters: 1 }))
    const onResult = vi.fn()
    const pv = createPreviewer({ fetchPreview, onResult })
    pv.request({ a: 1 }); vi.advanceTimersByTime(300)
    pv.request({ a: 2 }); vi.advanceTimersByTime(399)
    expect(fetchPreview).not.toHaveBeenCalled()
    vi.advanceTimersByTime(1); await flush()
    expect(fetchPreview).toHaveBeenCalledTimes(1)
    expect(fetchPreview.mock.calls[0][0]).toEqual({ a: 2 })
    expect(onResult).toHaveBeenCalledWith({ preview: { chapters: 1 } })
  })

  it('a stale preview never overwrites a newer one', async () => {
    const calls = []
    const fetchPreview = vi.fn((body, signal) => { const d = deferred(); calls.push({ body, signal, d }); return d.p })
    const onResult = vi.fn()
    const pv = createPreviewer({ fetchPreview, onResult })
    pv.request({ n: 1 }); vi.advanceTimersByTime(400)
    pv.request({ n: 2 }); vi.advanceTimersByTime(400)
    expect(calls).toHaveLength(2)
    expect(calls[0].signal.aborted).toBe(true)          // the older request was aborted
    calls[1].d.resolve({ chapters: 2 }); await flush()
    calls[0].d.resolve({ chapters: 1 }); await flush()  // late, and ignored
    calls[0].d.reject(err(400)); await flush()
    expect(onResult.mock.calls).toEqual([[{ preview: { chapters: 2 } }]])
  })

  it('request(null) and stop() cancel what is pending', async () => {
    const fetchPreview = vi.fn(async () => ({}))
    const onResult = vi.fn()
    const pv = createPreviewer({ fetchPreview, onResult })
    pv.request({ n: 1 }); pv.request(null); vi.advanceTimersByTime(1000)
    pv.request({ n: 2 }); pv.stop(); vi.advanceTimersByTime(1000); await flush()
    expect(fetchPreview).not.toHaveBeenCalled()
    expect(onResult).not.toHaveBeenCalled()
  })

  it('an error is passed on', async () => {
    const onResult = vi.fn()
    const pv = createPreviewer({ fetchPreview: async () => { throw err(400, 'since is out of range') }, onResult })
    pv.request({ n: 1 }); vi.advanceTimersByTime(400); await flush()
    expect(onResult.mock.calls[0][0].error.message).toBe('since is out of range')
  })
})

const row = (id, status, extra) => ({ id, title: 'T ' + id, status, progress: null, error: null, ...extra })

function stubApi({ list = [], get, make } = {}) {
  return {
    books: vi.fn(async () => ({ books: typeof list === 'function' ? list() : list })),
    bookGet: vi.fn(get || (async (id) => row(id, 'making', { progress: { done: 1, total: 4, stage: 'epub' } }))),
    bookMake: vi.fn(make || (async () => ({ id: 'new' }))),
  }
}

describe('createFollower', () => {
  it('a running book is picked up on open and followed to its end', async () => {
    let n = 0
    const api = stubApi({
      list: [row('b2', 'making'), row('b1', 'done')],
      get: async (id) => (++n < 3 ? row(id, 'making', { progress: { done: n, total: 4, stage: 'epub' } }) : row(id, 'done')),
    })
    const onJob = vi.fn(), onEnded = vi.fn(), onBooks = vi.fn()
    const f = createFollower({ api, onJob, onEnded, onBooks })
    await f.load(); await flush()
    expect(onBooks).toHaveBeenCalledWith([row('b2', 'making'), row('b1', 'done')])
    expect(f.id).toBe('b2')
    expect(api.bookGet).toHaveBeenCalledWith('b2')
    vi.advanceTimersByTime(1000); await flush()
    vi.advanceTimersByTime(1000); await flush()
    expect(onEnded).toHaveBeenCalledWith(row('b2', 'done'))
    expect(onJob.mock.calls.map(c => c[0].status)).toEqual(['making', 'making', 'done'])
    const asked = api.bookGet.mock.calls.length
    vi.advanceTimersByTime(5000); await flush()
    expect(api.bookGet.mock.calls.length).toBe(asked)     // ended: no more polling
    expect(api.books).toHaveBeenCalledTimes(2)            // the list is read again at the end
  })

  it('nothing is followed when no book is being made', async () => {
    const api = stubApi({ list: [row('b1', 'done'), row('b0', 'failed')] })
    const f = createFollower({ api })
    await f.load(); await flush()
    expect(f.id).toBe('')
    expect(api.bookGet).not.toHaveBeenCalled()
  })

  it('polling stops on stop() (the window closed)', async () => {
    const api = stubApi({ list: [row('b2', 'making')] })
    const onJob = vi.fn()
    const f = createFollower({ api, onJob })
    await f.load(); await flush()
    expect(api.bookGet).toHaveBeenCalledTimes(1)
    f.stop()
    vi.advanceTimersByTime(10000); await flush()
    expect(api.bookGet).toHaveBeenCalledTimes(1)
  })

  it('a reading that arrives after stop() is dropped', async () => {
    const d = deferred()
    const api = stubApi({ list: [row('b2', 'making')], get: () => d.p })
    const onJob = vi.fn()
    const f = createFollower({ api, onJob })
    await f.load(); await flush()
    f.stop()
    d.resolve(row('b2', 'done')); await flush()
    expect(onJob).not.toHaveBeenCalled()
  })

  it('a 409 makes it follow the running book', async () => {
    const api = stubApi({ list: [row('busy', 'making')], make: async () => { throw err(409, 'A book is being made; wait for it to finish') } })
    const f = createFollower({ api })
    await expect(f.make({ scope: { kind: 'all' } })).rejects.toMatchObject({ status: 409 })
    await flush()
    expect(api.books).toHaveBeenCalledTimes(1)
    expect(f.id).toBe('busy')
    expect(api.bookGet).toHaveBeenCalledWith('busy')
  })

  it('make() follows the new book at once', async () => {
    const api = stubApi({ list: () => [row('new', 'queued'), row('old', 'making')] })
    const onJob = vi.fn()
    const f = createFollower({ api, onJob })
    await expect(f.make({ scope: { kind: 'all' }, title: 'Castles' })).resolves.toBe('new')
    await flush()
    expect(onJob.mock.calls[0][0]).toEqual({ id: 'new', status: 'queued', title: 'Castles' })
    expect(f.id).toBe('new')                              // the list's other active row does not take over
    expect(api.bookGet.mock.calls.every(c => c[0] === 'new')).toBe(true)
  })

  it('a 404 while polling stops following; other errors are reported and polling goes on', async () => {
    let n = 0
    const api = stubApi({ list: [row('b2', 'making')], get: async () => { n++; throw n === 1 ? err(500, 'boom') : err(404, 'not found') } })
    const onJob = vi.fn(), onError = vi.fn()
    const f = createFollower({ api, onJob, onError })
    await f.load(); await flush()
    expect(onError).toHaveBeenCalledWith('poll', expect.objectContaining({ status: 500 }))
    vi.advanceTimersByTime(1000); await flush()
    expect(onJob).toHaveBeenCalledWith(null)
    expect(f.id).toBe('')
    vi.advanceTimersByTime(5000); await flush()
    expect(api.bookGet).toHaveBeenCalledTimes(2)
  })

  it('drop() stops following a deleted book; a list error is reported', async () => {
    const api = stubApi({ list: [row('b2', 'making')] })
    const onError = vi.fn()
    const f = createFollower({ api, onError })
    await f.load(); await flush()
    f.drop('b2')
    expect(f.id).toBe('')
    vi.advanceTimersByTime(5000); await flush()
    expect(api.bookGet).toHaveBeenCalledTimes(1)
    api.books.mockRejectedValueOnce(new TypeError('Failed to fetch'))
    await f.load()
    expect(onError).toHaveBeenCalledWith('list', expect.any(TypeError))
  })
})
