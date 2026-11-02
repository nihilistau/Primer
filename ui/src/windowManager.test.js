import { beforeEach, expect, test } from 'vitest'
import { open, close, focus, minimize, maximize, moveResize, setTitle, getWindows, _reset } from './windowManager.js'

beforeEach(() => _reset())

test('reader opens as separate instances; single apps reuse', () => {
  const a = open('reader'), b = open('reader')
  expect(a).not.toBe(b); expect(getWindows().filter(w => w.appId === 'reader')).toHaveLength(2)
  const g1 = open('graph', { single: true }), g2 = open('graph', { single: true })
  expect(g1).toBe(g2)
})
test('close removes only that instance', () => {
  const a = open('reader'), b = open('reader'); close(a)
  expect(getWindows().map(w => w.id)).toContain(b); expect(getWindows().map(w => w.id)).not.toContain(a)
})
test('instance ids count per app', () => {
  expect(open('reader')).toBe('reader:1'); expect(open('reader')).toBe('reader:2')
  expect(open('graph', { single: true })).toBe('graph:1')
})
test('a single app is restored and its props merged', () => {
  const id = open('graph', { single: true, props: { a: 1 } })
  minimize(id, { delay: 0 }); expect(getWindows()[0].minimized).toBe(true)
  open('graph', { single: true, props: { b: 2 } })
  const w = getWindows()[0]
  expect(w.minimized).toBe(false); expect(w.props).toEqual({ a: 1, b: 2 })
})
test('focus raises z, maximize toggles and remembers the box, title can change', () => {
  const a = open('reader'), b = open('reader')
  focus(a); const z = id => getWindows().find(w => w.id === id).z
  expect(z(a)).toBeGreaterThan(z(b))
  const before = { ...getWindows().find(w => w.id === a) }
  maximize(a); expect(getWindows().find(w => w.id === a).max).toBe(true)
  maximize(a)
  const w = getWindows().find(x => x.id === a)
  expect(w.max).toBe(false); expect([w.x, w.y, w.w, w.h]).toEqual([before.x, before.y, before.w, before.h])
  setTitle(a, 'Trebuchet'); expect(getWindows().find(x => x.id === a).title).toBe('Trebuchet')
  moveResize(a, { x: 5 }); expect(getWindows().find(x => x.id === a).x).toBe(5)
})
