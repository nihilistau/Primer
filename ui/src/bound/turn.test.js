import { describe, expect, test } from 'vitest'
import { STRIPS, curl, drag, dragProgress, finish, idle, jump, release, shade, shown, start, step, tick } from './turn.js'

const settle = (s) => { let k = 0; while (s.turning && k++ < 10000) s = tick(s, 16); return s }

describe('the turn state machine', () => {
  test('a drag released past the middle completes the turn', () => {
    let s = start(idle(3), 1, 10)
    expect(s).toEqual({ at: 3, turning: { dir: 1, progress: 0, target: null, mode: 'drag' } })
    s = drag(s, 0.7)
    expect(s.turning.progress).toBe(0.7)
    s = release(s)
    expect(s.turning.target).toBe(1)
    expect(settle(s)).toEqual({ at: 4, turning: null })
  })

  test('a drag released before the middle falls back', () => {
    const s = release(drag(start(idle(3), 1, 10), 0.4))
    expect(s.turning.target).toBe(0)
    expect(settle(s)).toEqual({ at: 3, turning: null })
    const back = release(drag(start(idle(3), -1, 10), 0.5))
    expect(settle(back)).toEqual({ at: 3, turning: null }) // exactly the middle is not past it
  })

  test('a drag backward turns to the spread before', () => {
    expect(settle(release(drag(start(idle(3), -1, 10), 0.9)))).toEqual({ at: 2, turning: null })
  })

  test('progress is clamped, and a released leaf no longer follows the pointer', () => {
    let s = drag(start(idle(0), 1, 4), 3)
    expect(s.turning.progress).toBe(1)
    s = drag(s, -2)
    expect(s.turning.progress).toBe(0)
    s = release(drag(s, 0.8))
    expect(drag(s, 0.1)).toBe(s)
    expect(release(s)).toBe(s)
  })

  test('nothing turns past either cover', () => {
    expect(start(idle(0), -1, 5)).toEqual({ at: 0, turning: null })
    expect(step(idle(4), 1, 5)).toEqual({ at: 4, turning: null })
    expect(step(idle(0), 1, 1)).toEqual({ at: 0, turning: null })
  })

  test('a step turns exactly one leaf, timed and eased', () => {
    let s = step(idle(2), 1, 10)
    expect(s.turning).toMatchObject({ dir: 1, target: 1, mode: 'step' })
    s = tick(s, 300)
    expect(s.turning.progress).toBeCloseTo(0.5)
    expect(shown(s.turning)).toBeCloseTo(0.5)
    expect(shown({ ...s.turning, progress: 0.25 })).toBeLessThan(0.25)
    expect(settle(s)).toEqual({ at: 3, turning: null })
  })

  test('one leaf per action under rapid input: each key finishes the turn before it', () => {
    let s = idle(0)
    for (let k = 0; k < 5; k++) s = tick(step(s, 1, 20), 16)
    expect(s.at).toBe(4)
    expect(s.turning).toMatchObject({ dir: 1 })
    expect(settle(s).at).toBe(5)
    // back and forth: each press is one leaf
    s = step(step(step(idle(5), 1, 20), -1, 20), -1, 20)
    expect(finish(s).at).toBe(4)
  })

  test('a step during a drag ends the drag as it was heading first', () => {
    let s = drag(start(idle(3), 1, 10), 0.8)
    s = step(s, 1, 10)
    expect(s.at).toBe(4)
    expect(finish(s).at).toBe(5)
    s = drag(start(idle(3), 1, 10), 0.2)
    expect(finish(step(s, 1, 10)).at).toBe(4)
  })

  test('jump goes straight to a spread, clamped, finishing any turn first', () => {
    expect(jump(idle(3), 0, 10)).toEqual({ at: 0, turning: null })
    expect(jump(idle(3), 99, 10)).toEqual({ at: 9, turning: null })
    expect(jump(step(idle(3), 1, 10), 7, 10)).toEqual({ at: 7, turning: null })
  })
})

describe('the curl', () => {
  test('six strips, flat at rest, bending more toward the free edge mid-turn', () => {
    const rest = curl(0, 1)
    expect(rest.deltas).toHaveLength(STRIPS)
    expect(rest.base).toBe(-0)
    expect(rest.deltas.every((d) => Math.abs(d) < 1e-9)).toBe(true)
    const mid = curl(0.5, 1)
    expect(mid.base).toBe(-90)
    for (let i = 1; i < STRIPS; i++) expect(Math.abs(mid.deltas[i])).toBeGreaterThan(Math.abs(mid.deltas[i - 1]))
    for (let i = 1; i < STRIPS; i++) expect(mid.angles[i]).toBeLessThan(mid.angles[i - 1]) // forward: the edge leads left
    const end = curl(1, 1)
    expect(end.base).toBe(-180)
    expect(end.angles.every((a) => Math.abs(a + 180) < 1e-9)).toBe(true)
  })

  test('going back the leaf starts on the left and its edge leads right', () => {
    expect(curl(0, -1).base).toBe(-180)
    expect(curl(1, -1).base).toBe(-0)
    const mid = curl(0.5, -1)
    for (let i = 1; i < STRIPS; i++) expect(mid.angles[i]).toBeGreaterThan(mid.angles[i - 1])
  })

  test('no strip turns past the spine', () => {
    for (let p = 0; p <= 1; p += 0.05) {
      for (const dir of [1, -1]) for (const a of curl(p, dir).angles) { expect(a).toBeLessThanOrEqual(0); expect(a).toBeGreaterThanOrEqual(-180) }
    }
  })

  test('shading: none lying flat, most edge-on', () => {
    expect(shade(0)).toBeCloseTo(0)
    expect(shade(-180)).toBeCloseTo(0)
    expect(shade(-90)).toBeGreaterThan(shade(-45))
  })

  test('the dragged edge follows the pointer', () => {
    expect(dragProgress(500, 100, 400, 1)).toBeCloseTo(0)   // at the right page's edge
    expect(dragProgress(100, 100, 400, 1)).toBeCloseTo(0.5) // at the spine
    expect(dragProgress(-300, 100, 400, 1)).toBeCloseTo(1)  // over the left page's edge
    expect(dragProgress(-300, 100, 400, -1)).toBeCloseTo(0)
    expect(dragProgress(500, 100, 400, -1)).toBeCloseTo(1)
    expect(dragProgress(900, 100, 400, 1)).toBe(0)          // clamped
    expect(dragProgress(0, 0, 0, 1)).toBe(0)
  })
})
