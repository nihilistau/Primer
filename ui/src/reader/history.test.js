import { expect, test } from 'vitest'
import { createHistory } from './history.js'

test('history back/forward and truncation', () => {
  const h = createHistory(); h.push({ nodeId: 'a' }); h.push({ nodeId: 'b' }); h.back()
  expect(h.current().nodeId).toBe('a'); expect(h.canForward()).toBe(true)
  h.push({ nodeId: 'c' }); expect(h.canForward()).toBe(false)
})
test('empty history', () => {
  const h = createHistory()
  expect(h.current()).toBe(null); expect(h.canBack()).toBe(false); expect(h.back()).toBe(null)
})
test('replace swaps the current entry', () => {
  const h = createHistory(); h.push({ nodeId: 'a' }); h.push({ nodeId: 'b' }); h.replace({ nodeId: 'b', title: 'B' })
  expect(h.current().title).toBe('B'); h.back(); expect(h.current().nodeId).toBe('a')
})
