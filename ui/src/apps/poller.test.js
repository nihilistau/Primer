import { expect, test } from 'vitest'
import { createPoller } from './poller.js'

const deferred = () => { let res, rej; const p = new Promise((a, b) => { res = a; rej = b }); return { p, res, rej } }
const flush = () => new Promise(r => setTimeout(r, 0))

function setup() {
  const calls = []
  const seen = []
  const errors = []
  const poller = createPoller(() => { const d = deferred(); calls.push(d); return d.p },
    d => seen.push(d), e => errors.push(e.message))
  return { calls, seen, errors, poller }
}

test('a tick while a request is in flight is skipped', async () => {
  const { calls, poller } = setup()
  poller.tick(); poller.tick()
  expect(calls).toHaveLength(1)
  calls[0].res('a'); await flush()
  poller.tick()
  expect(calls).toHaveLength(2)
})

test('refresh during an in-flight poll fetches again and the stale response is discarded', async () => {
  const { calls, seen, poller } = setup()
  poller.tick()                          // in flight, served before the change
  poller.refresh()                       // after the change
  expect(calls).toHaveLength(2)
  calls[1].res('new'); await flush()
  calls[0].res('stale'); await flush()   // arrives late
  expect(seen).toEqual(['new'])
})

test('a stale failure is discarded too', async () => {
  const { calls, errors, poller } = setup()
  poller.tick(); poller.refresh()
  calls[0].rej(new Error('old')); await flush()
  expect(errors).toEqual([])
})

test('nothing is delivered after stop', async () => {
  const { calls, seen, poller } = setup()
  poller.tick(); poller.stop()
  calls[0].res('x'); await flush()
  expect(seen).toEqual([])
  poller.refresh()
  expect(calls).toHaveLength(1)
})
