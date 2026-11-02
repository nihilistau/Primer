import { describe, it, expect, vi, beforeEach } from 'vitest'

let impl
vi.mock('../api.js', () => ({ intent: (...a) => impl(...a) }))
import { routePrompt, imageButton, thumbState, imageChecking, imageCheckFailed } from './imageRoute.js'

beforeEach(() => { impl = null })

describe('routePrompt', () => {
  it('routes an image request on a page to a node_id image body', async () => {
    impl = async () => ({ image: 'a trebuchet' })
    expect(await routePrompt('draw a trebuchet', { nodeId: 'n1' }))
      .toEqual({ kind: 'image', body: { node_id: 'n1', subject: 'a trebuchet' } })
  })
  it('uses concept when the reader is empty', async () => {
    impl = async () => ({ image: 'a trebuchet' })
    expect(await routePrompt('draw a trebuchet', { nodeId: null }))
      .toEqual({ kind: 'image', body: { concept: 'a trebuchet', subject: 'a trebuchet' } })
  })
  it('routes a video request to a kind video body, before an image', async () => {
    impl = async () => ({ video: 'a mill turning', image: null })
    expect(await routePrompt('make a video of a mill turning', { nodeId: 'n1' }))
      .toEqual({ kind: 'video', body: { node_id: 'n1', subject: 'a mill turning', kind: 'video' } })
    impl = async () => ({ video: 'a mill', image: 'a mill' })
    expect(await routePrompt('animate a mill', { nodeId: null }))
      .toEqual({ kind: 'video', body: { concept: 'a mill', subject: 'a mill', kind: 'video' } })
  })
  it('routes anything else to a page, with from_id when there is a node', async () => {
    impl = async () => ({ image: null })
    expect(await routePrompt('how do levers work', { nodeId: 'n1' }))
      .toEqual({ kind: 'page', body: { concept: 'how do levers work', via: 'typed', from_id: 'n1' } })
    expect(await routePrompt('how do levers work', { nodeId: null }))
      .toEqual({ kind: 'page', body: { concept: 'how do levers work', via: 'typed' } })
  })
  it('falls back to a page when the intent call fails', async () => {
    impl = async () => { throw new Error('down') }
    let r; try { r = await routePrompt('draw x', { nodeId: null }) } catch (e) { throw new Error('THROWN FROM routePrompt: ' + e.message) }
    expect(r.kind).toBe('page')
  })
})

describe('imageButton', () => {
  it('enabled when available', () => expect(imageButton({ image_available: { ok: true, reason: '' } })).toEqual({ enabled: true, reason: '' }))
  it('disabled with the reason', () =>
    expect(imageButton({ image_available: { ok: false, reason: 'No image model' } })).toEqual({ enabled: false, reason: 'No image model' }))
  it('disabled with a default reason when status is missing', () => {
    expect(imageButton(null).enabled).toBe(false)
    expect(imageButton(null).reason).toBeTruthy()
    expect(imageButton({ image_available: { ok: false } }).reason).toBeTruthy()
  })
})

describe('image button states', () => {
  it('checking is neutral and disabled', () => {
    expect(imageChecking.enabled).toBe(false)
    expect(imageChecking.checking).toBe(true)
  })
  it('a failed status call says so, with its reason', () => {
    const b = imageCheckFailed(new Error('boom'))
    expect(b.enabled).toBe(false)
    expect(b.reason).toBe("Couldn't check image availability: boom")
    expect(imageCheckFailed(null).reason).toMatch(/^Couldn't check image availability: /)
  })
})

describe('thumbState', () => {
  it('classifies statuses', () => {
    expect(thumbState({ status: 'done' })).toBe('done')
    expect(thumbState({ status: 'failed' })).toBe('failed')
    expect(thumbState({ status: 'cancelled' })).toBe('failed')
    for (const s of ['prompting', 'queued', 'loading', 'generating']) expect(thumbState({ status: s })).toBe('busy')
  })
})
