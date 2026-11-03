import { describe, expect, it } from 'vitest'
import { chunkAt, initial, next, readyList, waitingFor } from './storyPlayer.js'

const run = (s, ...events) => events.reduce(next, s)

describe('storyPlayer', () => {
  it('waits for scene 1 when played before it is ready, then plays by itself', () => {
    let s = run(initial([false, false, false]), { type: 'play' })
    expect(s).toMatchObject({ scene: 0, playing: false, waiting: true })
    expect(waitingFor(s)).toBe(1)
    s = next(s, { type: 'ready', ready: [true, false, false] })
    expect(s).toMatchObject({ scene: 0, playing: true, waiting: false })
  })

  it('plays scene 1 straight away when its audio is ready', () => {
    const s = run(initial([true, false]), { type: 'play' })
    expect(s).toMatchObject({ scene: 0, playing: true, waiting: false })
  })

  it('goes on to a ready successor when a scene ends', () => {
    const s = run(initial([true, true, false]), { type: 'play' }, { type: 'ended' })
    expect(s).toMatchObject({ scene: 1, playing: true, waiting: false })
  })

  it('waits at the end of a scene whose successor is not ready, then continues when it is', () => {
    let s = run(initial([true, false, false]), { type: 'play' }, { type: 'ended' })
    expect(s).toMatchObject({ scene: 1, playing: false, waiting: true })
    expect(waitingFor(s)).toBe(2)
    s = next(s, { type: 'ready', ready: [true, false, false] })       // nothing new: still waiting
    expect(s).toMatchObject({ scene: 1, playing: false, waiting: true })
    s = next(s, { type: 'ready', ready: [true, true, false] })
    expect(s).toMatchObject({ scene: 1, playing: true, waiting: false })
  })

  it('stops after the last scene, and Play then starts over', () => {
    let s = run(initial([true, true]), { type: 'play' }, { type: 'ended' }, { type: 'ended' })
    expect(s).toMatchObject({ scene: 1, playing: false, waiting: false, finished: true })
    s = next(s, { type: 'play' })
    expect(s).toMatchObject({ scene: 0, playing: true, finished: false })
  })

  it('pauses and resumes in the same scene', () => {
    let s = run(initial([true, true]), { type: 'play' }, { type: 'ended' }, { type: 'pause' })
    expect(s).toMatchObject({ scene: 1, playing: false, waiting: false })
    s = next(s, { type: 'play' })
    expect(s).toMatchObject({ scene: 1, playing: true })
  })

  it('pausing while waiting cancels the wait', () => {
    let s = run(initial([false]), { type: 'play' }, { type: 'pause' })
    expect(s).toMatchObject({ playing: false, waiting: false })
    s = next(s, { type: 'ready', ready: [true] })
    expect(s.playing).toBe(false)
  })

  it('seeks to a scene, keeping the wish to play, and clamps', () => {
    let s = run(initial([true, true, false]), { type: 'play' }, { type: 'seek', scene: 1 })
    expect(s).toMatchObject({ scene: 1, playing: true })
    s = next(s, { type: 'seek', scene: 2 })
    expect(s).toMatchObject({ scene: 2, playing: false, waiting: true })
    s = next(s, { type: 'seek', scene: 99 })
    expect(s.scene).toBe(2)
    s = next(s, { type: 'seek', scene: -4 })
    expect(s.scene).toBe(0)
    const paused = run(initial([true, true]), { type: 'seek', scene: 1 })
    expect(paused).toMatchObject({ scene: 1, playing: false, waiting: false })
  })

  it('ignores an ended event while not playing (a late audio event)', () => {
    const s = run(initial([true, true]), { type: 'ended' })
    expect(s).toMatchObject({ scene: 0, playing: false })
  })

  it('reset starts over for a new story', () => {
    const s = run(initial([true, true]), { type: 'play' }, { type: 'ended' }, { type: 'reset' })
    expect(s).toMatchObject({ scene: 0, playing: false, waiting: false, ready: [] })
  })
})

describe('readyList', () => {
  it('marks a scene ready once it is done', () => {
    expect(readyList([{ status: 'done' }, { status: 'narrating' }, { status: 'waiting' }, { status: 'failed' }]))
      .toEqual([true, false, false, false])
    expect(readyList(undefined)).toEqual([])
  })
})

describe('chunkAt', () => {
  const chunks = [[0, 10, 0, 2], [11, 25, 2.25, 5], [26, 40, 5.25, 8]]
  it('is the chunk whose start is the latest one at or before the time', () => {
    expect(chunkAt(chunks, 0)).toBe(0)
    expect(chunkAt(chunks, 1.9)).toBe(0)
    expect(chunkAt(chunks, 2.1)).toBe(0)            // in the gap: the sentence just spoken stays lit
    expect(chunkAt(chunks, 2.25)).toBe(1)
    expect(chunkAt(chunks, 7)).toBe(2)
    expect(chunkAt(chunks, 99)).toBe(2)
  })
  it('is -1 with no chunks, and 0 before the first start', () => {
    expect(chunkAt([], 3)).toBe(-1)
    expect(chunkAt(undefined, 3)).toBe(-1)
    expect(chunkAt([[0, 5, 0.5, 2]], 0.1)).toBe(0)
  })
})
