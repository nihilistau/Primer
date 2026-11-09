import { describe, it, expect, vi, beforeEach } from 'vitest'

let impl
vi.mock('../api.js', () => ({ intent: (...a) => impl(...a) }))
import { timelineButton, timelineChecking, timelineCheckFailed, diagramButton, diagramChecking, diagramCheckFailed } from './imageRoute.js'
import { routePrompt, storyButton, storyChecking, mapButton, typedMapBody, imageButton, thumbState, imageChecking, imageCheckFailed } from './imageRoute.js'

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
  it('routes a map request to the place as a typed page, then its map', async () => {
    impl = async () => ({ map: 'Warwick', image: null, video: null })
    expect(await routePrompt('show Warwick on a map', { nodeId: 'n1' }))
      .toEqual({ kind: 'map', body: { concept: 'Warwick', via: 'typed', from_id: 'n1' } })
    expect(await routePrompt('map of Warwick', { nodeId: null }))
      .toEqual({ kind: 'map', body: { concept: 'Warwick', via: 'typed' } })
  })
  it('routes a story request to the topic as a typed page, then its story', async () => {
    impl = async () => ({ story: 'the Norman conquest', map: null, image: null, video: null })
    expect(await routePrompt('tell me a story about the Norman conquest', { nodeId: 'n1' }))
      .toEqual({ kind: 'story', body: { concept: 'the Norman conquest', via: 'typed', from_id: 'n1' } })
    expect(await routePrompt('a story about owls', { nodeId: null }))
      .toEqual({ kind: 'story', body: { concept: 'the Norman conquest', via: 'typed' } })
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

describe('mapButton', () => {
  it('is enabled when the map is available', () => {
    expect(mapButton({ map_available: { ok: true, reason: '' } })).toEqual({ enabled: true, reason: '' })
  })
  it('carries the reason when it is not', () => {
    expect(mapButton({ map_available: { ok: false, reason: 'No map file.' } })).toEqual({ enabled: false, reason: 'No map file.' })
    expect(mapButton({}).enabled).toBe(false)
    expect(mapButton({}).reason).toMatch(/not available/)
  })
})

describe('typedMapBody', () => {
  it('is a phrase map of the typed subject', () => {
    expect(typedMapBody('n2', 'Paris')).toEqual({ node_id: 'n2', phrase: 'Paris', subject: 'Paris' })
  })
})

describe('storyButton', () => {
  it('enabled when the voice is available', () =>
    expect(storyButton({ voice_available: { ok: true, reason: '' } })).toEqual({ enabled: true, reason: '' }))
  it('disabled with the reason', () =>
    expect(storyButton({ voice_available: { ok: false, reason: 'The voice server is not running' } }))
      .toEqual({ enabled: false, reason: 'The voice server is not running' }))
  it('disabled with a default reason when status is missing', () => {
    expect(storyButton(null).enabled).toBe(false)
    expect(storyButton({ voice_available: { ok: false } }).reason).toBeTruthy()
    expect(storyChecking.enabled).toBe(false)
  })
})

describe('timeline and diagram requests', () => {
  it('routes a timeline request to the subject as a typed page, then its timeline', async () => {
    impl = async () => ({ image: null, video: null, map: null, story: null, timeline: 'the Tudors', diagram: null })
    expect(await routePrompt('timeline of the Tudors', { nodeId: 'n1' }))
      .toEqual({ kind: 'timeline', body: { concept: 'the Tudors', via: 'typed', from_id: 'n1' } })
    expect(await routePrompt('timeline of the Tudors', { nodeId: null }))
      .toEqual({ kind: 'timeline', body: { concept: 'the Tudors', via: 'typed' } })
  })
  it('routes a diagram request to the subject as a typed page, then its diagram, before anything else', async () => {
    impl = async () => ({ image: 'x', video: null, map: 'y', story: 'z', timeline: 'w', diagram: 'a trebuchet' })
    expect(await routePrompt('diagram of a trebuchet', { nodeId: 'n1' }))
      .toEqual({ kind: 'diagram', body: { concept: 'a trebuchet', via: 'typed', from_id: 'n1' } })
  })
  it('a timeline outranks a story or a map, as the server checks them', async () => {
    impl = async () => ({ map: 'Rome', story: 'Rome', timeline: 'Rome' })
    expect((await routePrompt('Rome timeline', {})).kind).toBe('timeline')
  })
  it('timelineButton and diagramButton read their availability with the reason', () => {
    expect(timelineButton({ timeline_available: { ok: true, reason: '' } })).toEqual({ enabled: true, reason: '' })
    expect(timelineButton({ timeline_available: { ok: false, reason: 'No fast model in this tier' } }))
      .toEqual({ enabled: false, reason: 'No fast model in this tier' })
    expect(timelineButton({}).reason).toMatch(/not available/)
    expect(diagramButton({ diagram_available: { ok: true, reason: '' } })).toEqual({ enabled: true, reason: '' })
    expect(diagramButton({ diagram_available: { ok: false, reason: 'No writing model' } }))
      .toEqual({ enabled: false, reason: 'No writing model' })
    expect(diagramButton(null).enabled).toBe(false)
    expect(timelineChecking.enabled).toBe(false)
    expect(diagramCheckFailed(new Error('down')).reason).toMatch(/down/)
    expect(timelineCheckFailed(new Error('down')).reason).toMatch(/down/)
    expect(diagramChecking.checking).toBe(true)
  })
})
