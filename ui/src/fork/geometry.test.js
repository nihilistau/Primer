import { describe, expect, test } from 'vitest'
import { layoutFork } from './geometry.js'

describe('layoutFork', () => {
  test('three cards below anchor, inside container, not overlapping', () => {
    const { cards, paths } = layoutFork({ x: 100, y: 80, w: 90, h: 20 }, { w: 640, h: 500 }, 3)
    expect(cards).toHaveLength(3); expect(paths).toHaveLength(3)
    cards.forEach(c => { expect(c.x).toBeGreaterThanOrEqual(0); expect(c.x + 200).toBeLessThanOrEqual(640); expect(c.y).toBeGreaterThan(100) })
    expect(cards[1].x).toBeGreaterThanOrEqual(cards[0].x + 200)
    expect(cards[2].x).toBeGreaterThanOrEqual(cards[1].x + 200)
  })
  test('flips above when anchor is near the bottom', () => {
    const { cards, paths } = layoutFork({ x: 100, y: 460, w: 90, h: 20 }, { w: 640, h: 500 }, 3)
    cards.forEach(c => expect(c.y + 64).toBeLessThanOrEqual(460))
    expect(paths[0].startsWith('M145 460')).toBe(true)          // from the anchor's top-centre
  })
  test('narrow container stacks without overflow or overlap', () => {
    const { cards } = layoutFork({ x: 10, y: 40, w: 60, h: 20 }, { w: 360, h: 600 }, 3)
    cards.forEach(c => { expect(c.x).toBeGreaterThanOrEqual(0); expect(c.x + 200).toBeLessThanOrEqual(360) })
    expect(cards[1].y).toBeGreaterThanOrEqual(cards[0].y + 64)
    expect(cards[2].y).toBeGreaterThanOrEqual(cards[1].y + 64)
  })
  test('paths run from the anchor bottom-centre to each card top-centre', () => {
    const { cards, paths } = layoutFork({ x: 100, y: 80, w: 90, h: 20 }, { w: 640, h: 500 }, 3)
    expect(paths[0].startsWith('M145 100 C')).toBe(true)
    expect(paths[1].endsWith(`${cards[1].x + 100} ${cards[1].y}`)).toBe(true)
  })
  test('two cards and one card', () => {
    expect(layoutFork({ x: 100, y: 80, w: 90, h: 20 }, { w: 640, h: 500 }, 2).cards).toHaveLength(2)
    expect(layoutFork({ x: 100, y: 80, w: 90, h: 20 }, { w: 640, h: 500 }, 1).paths).toHaveLength(1)
  })
})

/* the geometry BranchFork actually ships: card height 80 */
const C80 = { w: 200, h: 80 }
describe('layoutFork edges', () => {
  test('h:80 row stays inside the container, actions row too', () => {
    const { cards, actions } = layoutFork({ x: 100, y: 80, w: 90, h: 20 }, { w: 640, h: 500 }, 3, C80)
    cards.forEach(c => { expect(c.x).toBeGreaterThanOrEqual(0); expect(c.x + 200).toBeLessThanOrEqual(640); expect(c.y + 80).toBeLessThanOrEqual(500) })
    expect(actions.y).toBeGreaterThanOrEqual(cards[0].y + 80)
    expect(actions.y + 32).toBeLessThanOrEqual(500)
    expect(actions.x + actions.w).toBeLessThanOrEqual(640)
  })
  test('h:80 flips above with the actions row inside the container', () => {
    const { cards, actions } = layoutFork({ x: 100, y: 440, w: 90, h: 20 }, { w: 640, h: 500 }, 3, C80)
    cards.forEach(c => expect(c.y + 80).toBeLessThanOrEqual(440))
    expect(actions.flipped).toBe(true)
    expect(actions.y).toBeGreaterThanOrEqual(0)
  })
  test('one and two cards are centred on the anchor, clamped at both edges', () => {
    const mid = layoutFork({ x: 300, y: 80, w: 40, h: 20 }, { w: 640, h: 500 }, 1, C80)
    expect(mid.cards[0].x).toBe(320 - 100)
    const two = layoutFork({ x: 300, y: 80, w: 40, h: 20 }, { w: 640, h: 500 }, 2, C80)
    expect(two.cards[0].x + 200 + 12).toBe(two.cards[1].x)
    expect((two.cards[0].x + two.cards[1].x + 200) / 2).toBe(320)   // the row is centred on the anchor
    expect(layoutFork({ x: 0, y: 80, w: 20, h: 20 }, { w: 640, h: 500 }, 1, C80).cards[0].x).toBe(8)
    expect(layoutFork({ x: 620, y: 80, w: 20, h: 20 }, { w: 640, h: 500 }, 1, C80).cards[0].x).toBe(640 - 200 - 8)
  })
  test('a container narrower than a card pins cards at the margin', () => {
    const { cards } = layoutFork({ x: 10, y: 40, w: 20, h: 20 }, { w: 100, h: 600 }, 1, { w: 120, h: 80 })
    expect(cards[0].x).toBe(8)
  })
  test('neither side fits: cards and actions are clamped inside the container', () => {
    const { cards, actions } = layoutFork({ x: 100, y: 250, w: 60, h: 20 }, { w: 380, h: 550 }, 3, C80)
    cards.forEach(c => { expect(c.y).toBeGreaterThanOrEqual(0); expect(c.y + 80).toBeLessThanOrEqual(550) })
    expect(actions.y + 32).toBeLessThanOrEqual(550)
  })
  test('an anchor above the container keeps the cards on screen', () => {
    const { cards } = layoutFork({ x: 100, y: -40, w: 90, h: 20 }, { w: 640, h: 500 }, 3, C80)
    cards.forEach(c => { expect(c.y).toBeGreaterThanOrEqual(0); expect(c.y + 80).toBeLessThanOrEqual(500) })
  })
  test('a block taller than the container is pinned to the top', () => {
    const { cards } = layoutFork({ x: 10, y: 20, w: 40, h: 20 }, { w: 360, h: 200 }, 3, C80)
    expect(cards[0].y).toBe(0)
  })
  test('a flipped column stays inside and keeps the cards clear of the anchor', () => {
    const { cards, column } = layoutFork({ x: 10, y: 560, w: 60, h: 20 }, { w: 360, h: 600 }, 3, C80)
    expect(column).toBe(true)
    cards.forEach(c => { expect(c.y).toBeGreaterThanOrEqual(0); expect(c.y + 80).toBeLessThanOrEqual(560) })
  })
  test('column curves fan out: distinct, ending on the near edge at their own card', () => {
    const anchor = { x: 10, y: 40, w: 60, h: 20 }
    const { cards, paths } = layoutFork(anchor, { w: 360, h: 700 }, 3, C80)
    expect(new Set(paths).size).toBe(3)
    paths.forEach((d, i) => {
      const [ex, ey] = d.split(', ').pop().split(' ').map(Number)
      expect(ex).toBe(cards[i].x)                       // the anchor is left of the column
      expect(ey).toBe(cards[i].y + 40)
      cards.forEach((c, j) => { if (j !== i) expect(ey > c.y && ey < c.y + 80 && ex > c.x && ex < c.x + 200).toBe(false) })
    })
    expect(cards[0].x).toBeGreaterThan(anchor.x + anchor.w)   // beside the anchor, not under it
  })
  test('an anchor on the right puts the column on the left, curves reaching its right edge', () => {
    const { cards, paths } = layoutFork({ x: 290, y: 40, w: 60, h: 20 }, { w: 360, h: 700 }, 3, C80)
    expect(cards[0].x).toBe(8)
    paths.forEach(d => expect(Number(d.split(', ').pop().split(' ')[0])).toBe(208))
  })
})
