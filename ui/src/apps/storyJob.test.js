import { describe, expect, it } from 'vitest'
import { isSettled, sceneSegments, storyStatusLabel, waitingLabel } from './storyJob.js'

const sc = (status, extra = {}) => ({ idx: 0, title: 't', status, picture: null, ...extra })

describe('storyStatusLabel', () => {
  it('says what it is doing', () => {
    expect(storyStatusLabel(null)).toBe('Starting…')
    expect(storyStatusLabel({ kind: 'story', status: 'writing', scenes: [] })).toBe('Writing the story…')
    expect(storyStatusLabel({ kind: 'listen', status: 'writing', scenes: [] })).toBe('Getting the page ready…')
  })
  it('names the scene being narrated', () => {
    const s = { kind: 'story', status: 'narrating', scenes: [sc('done'), sc('narrating'), sc('waiting'), sc('waiting')] }
    expect(storyStatusLabel(s)).toBe('Narrating scene 2 of 4…')
    expect(storyStatusLabel({ kind: 'listen', status: 'narrating', scenes: [sc('narrating')] })).toBe('Narrating…')
  })
  it('reports done and failed with the reason', () => {
    expect(storyStatusLabel({ kind: 'story', status: 'done', scenes: [sc('done'), sc('done'), sc('done')] })).toBe('3 scenes')
    expect(storyStatusLabel({ kind: 'story', status: 'done', scenes: [sc('done')] })).toBe('1 scene')
    expect(storyStatusLabel({ kind: 'listen', status: 'done', scenes: [sc('done')] })).toBe('Ready')
    expect(storyStatusLabel({ status: 'failed', error: 'voice down', scenes: [] })).toBe('Failed: voice down')
    expect(storyStatusLabel({ status: 'failed', scenes: [] })).toBe('Failed: unknown error')
  })
})

describe('isSettled', () => {
  it('needs the story ended and no picture still being made', () => {
    expect(isSettled(null)).toBe(false)
    expect(isSettled({ status: 'narrating', scenes: [] })).toBe(false)
    expect(isSettled({ status: 'done', scenes: [sc('done', { picture: { status: 'generating' } })] })).toBe(false)
    expect(isSettled({ status: 'done', scenes: [sc('done', { picture: { status: 'done' } }), sc('done')] })).toBe(true)
    expect(isSettled({ status: 'failed', scenes: [sc('failed', { picture: { status: 'failed' } })] })).toBe(true)
  })
})

describe('waitingLabel', () => {
  it('says which scene it waits for', () => {
    expect(waitingLabel(2, { status: 'narrating', scenes: [] })).toBe('Waiting for scene 2…')
  })
  it('says so when the scene will never come', () => {
    expect(waitingLabel(2, { status: 'failed', error: 'voice down', scenes: [sc('done'), sc('failed')] }))
      .toBe('Scene 2 was not narrated: voice down')
    expect(waitingLabel(1, { status: 'failed', scenes: [] })).toBe('Scene 1 was not narrated: unknown error')
  })
})

describe('sceneSegments', () => {
  const spoken = 'One two. Three four. Five six.'
  const chunks = [[0, 8, 0, 1], [9, 20, 1.25, 2], [21, 30, 2.25, 3]]
  const text = (segs) => segs.map(s => s.text).join('')
  it('cuts the text before, at and after the current chunk, keeping every character', () => {
    const segs = sceneSegments(spoken, chunks, 1, [])
    expect(text(segs)).toBe(spoken)
    expect(segs.filter(s => s.part === 'now').map(s => s.text)).toEqual(['Three four.'])
    expect(segs[0]).toMatchObject({ part: 'before', text: 'One two. ' })
  })
  it('marks phrases (the first time) in any part', () => {
    const segs = sceneSegments(spoken, chunks, 1, ['four', 'six'])
    expect(segs.filter(s => s.phrase).map(s => [s.phrase, s.part])).toEqual([['four', 'now'], ['six', 'after']])
    expect(text(segs)).toBe(spoken)
  })
  it('keeps a phrase clickable across a chunk edge, cut into its two parts', () => {
    const segs = sceneSegments(spoken, chunks, 1, ['two. Three'])
    expect(text(segs)).toBe(spoken)
    const marked = segs.filter(s => s.phrase)
    expect(marked.every(s => s.phrase === 'two. Three')).toBe(true)
    expect(marked.map(s => s.text).join('')).toBe('two. Three')
    expect(new Set(marked.map(s => s.part))).toEqual(new Set(['before', 'now']))
  })
  it('is plain text when no chunk is current or the ranges do not fit', () => {
    expect(sceneSegments(spoken, [], -1, [])).toEqual([{ text: spoken, part: 'after' }])
    expect(text(sceneSegments(spoken, [[0, 999, 0, 1]], 0, []))).toBe(spoken)
    expect(text(sceneSegments(spoken, [[5, 2, 0, 1]], 0, []))).toBe(spoken)
    expect(sceneSegments('', [], -1, [])).toEqual([])
  })
})
