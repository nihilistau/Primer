import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { describeJob, pollJob, previewUrl, settingsSummary } from './imageJob.js'

describe('describeJob', () => {
  const d = (status, extra = {}) => describeJob({ status, ...extra })

  it('labels every status', () => {
    expect(d('prompting')).toMatchObject({ label: 'Writing the prompt…', done: false, failed: false, canRetry: false })
    expect(d('queued').label).toBe('Waiting for the GPU')
    expect(d('loading').label).toBe('Loading the model…')
    expect(d('done')).toMatchObject({ label: 'Done', pct: 100, done: true })
    expect(d('cancelled')).toMatchObject({ label: 'Cancelled', done: true, failed: false, canRetry: false })
  })

  it('reports generating progress as steps and a percentage', () => {
    expect(d('generating', { progress: { step: 7, total: 20 } })).toMatchObject({ label: 'Step 7 of 20', pct: 35 })
    expect(d('generating', { progress: { step: 19, total: 20 } }).label).toBe('Step 19 of 20')
    expect(d('generating').label).toBe('Generating…')
    expect(d('generating', { progress: { step: 0, total: 0 } }).pct).toBe(0)
  })

  it('once the last step is done, says it is decoding with the bar full', () => {
    expect(d('generating', { progress: { step: 2, total: 2, stage: 'high' } })).toMatchObject({ label: 'High-noise step 2 of 2', pct: 100, done: false })
    expect(d('generating', { progress: { step: 1, total: 2, stage: 'high' } })).toMatchObject({ label: 'High-noise step 1 of 2', pct: 50 })
    expect(d('generating', { progress: { step: 1, total: 4, stage: 'low' } })).toMatchObject({ label: 'Step 1 of 4', pct: 25 })
    expect(d('generating', { progress: { step: 4, total: 4, stage: 'low' } })).toMatchObject({ label: 'Decoding…', pct: 100 })
    expect(d('generating', { progress: { step: 20, total: 20 } })).toMatchObject({ label: 'Decoding…', pct: 100, done: false })
    expect(d('generating', { progress: { step: 21, total: 20 } })).toMatchObject({ label: 'Decoding…', pct: 100 })
    expect(d('generating', { progress: { step: 0, total: 0 } }).label).toBe('Generating…')
  })

  it('failed carries the reason and can retry', () => {
    expect(d('failed', { error: 'out of memory' })).toMatchObject(
      { label: 'Failed: out of memory', failed: true, done: true, canRetry: true })
    expect(d('failed').label).toBe('Failed: unknown error')
  })
})

describe('settingsSummary', () => {
  it('reads size, steps, cfg and sampler', () => {
    expect(settingsSummary({ width: 1024, height: 768, steps: 20, cfg: 7, sampler: 'euler', seed: 5 }))
      .toBe('1024×768 · 20 steps · cfg 7 · euler')
    expect(settingsSummary({})).toBe('')
  })
})

describe('pollJob', () => {
  beforeEach(() => { vi.useFakeTimers() })
  afterEach(() => { vi.useRealTimers() })

  const script = (statuses) => {
    let i = 0
    return vi.fn(async () => ({ id: 'm', status: statuses[Math.min(i++, statuses.length - 1)] }))
  }

  it('stops once the job is done, failed or cancelled', async () => {
    for (const end of ['done', 'failed', 'cancelled']) {
      const fetchJob = script(['queued', 'generating', end])
      const seen = []
      const p = pollJob('m', j => seen.push(j.status), { interval: 1000, fetchJob })
      await vi.advanceTimersByTimeAsync(10000)
      expect((await p).status).toBe(end)
      expect(seen).toEqual(['queued', 'generating', end])
      expect(fetchJob).toHaveBeenCalledTimes(3)
    }
  })

  it('stops when aborted', async () => {
    const fetchJob = script(['generating'])
    const ac = new AbortController()
    const p = pollJob('m', () => {}, { interval: 1000, fetchJob, signal: ac.signal })
    await vi.advanceTimersByTimeAsync(2500)
    const n = fetchJob.mock.calls.length
    ac.abort()
    await vi.advanceTimersByTimeAsync(5000)
    expect(fetchJob.mock.calls.length).toBe(n)
    expect(await p).toBeUndefined()
  })

  it('reports a failed poll and keeps going', async () => {
    let i = 0
    const fetchJob = vi.fn(async () => { if (i++ === 0) throw new Error('net'); return { status: 'done' } })
    const errs = []
    const p = pollJob('m', () => {}, { interval: 1000, fetchJob, onError: e => errs.push(e.message) })
    await vi.advanceTimersByTimeAsync(3000)
    expect((await p).status).toBe('done')
    expect(errs).toEqual(['net'])
  })
})

describe('previewUrl', () => {
  it('builds a data URL only from clean base64', () => {
    expect(previewUrl('iVBORw0KGgo=')).toBe('data:image/png;base64,iVBORw0KGgo=')
    for (const bad of ['', null, undefined, 'a"onerror="x', 'a b', 'ab=c', 'a\nb', 5]) expect(previewUrl(bad)).toBe('')
  })
})

describe('pollJob on a vanished job', () => {
  beforeEach(() => { vi.useFakeTimers() })
  afterEach(() => { vi.useRealTimers() })

  it('stops polling on a 404 and reports it', async () => {
    const fetchJob = vi.fn(async () => { throw Object.assign(new Error('not found'), { status: 404 }) })
    const errs = []
    const p = pollJob('m', () => {}, { interval: 1000, fetchJob, onError: e => errs.push(e.status) })
    await vi.advanceTimersByTimeAsync(5000)
    expect(await p).toBeUndefined()
    expect(fetchJob).toHaveBeenCalledTimes(1)
    expect(errs).toEqual([404])
  })
})
