import { expect, test } from 'vitest'
import { splitCitations } from './citations.js'

test('a valid [n] becomes a citation between text', () => {
  expect(splitCitations('Rome fell [1] later.', 2)).toEqual(['Rome fell ', { n: 1 }, ' later.'])
})
test('out of range stays text', () => {
  expect(splitCitations('x [3] y', 2)).toEqual(['x [3] y'])
  expect(splitCitations('x [0] y', 2)).toEqual(['x [0] y'])
})
test('lists, footnotes and non-numbers stay text', () => {
  expect(splitCitations('a [1, 2] b', 2)).toEqual(['a [1, 2] b'])
  expect(splitCitations('a [^1] b', 2)).toEqual(['a [^1] b'])
  expect(splitCitations('a [x] b', 2)).toEqual(['a [x] b'])
})
test('adjacent citations', () => {
  expect(splitCitations('a[1][2]', 2)).toEqual(['a', { n: 1 }, { n: 2 }])
})
test('no sources means plain text', () => {
  expect(splitCitations('a [1]', 0)).toEqual(['a [1]'])
  expect(splitCitations('', 2)).toEqual([])
})

import { safeHref } from './citations.js'
test('safeHref allows only http(s)', () => {
  expect(safeHref('https://a/b')).toBe('https://a/b')
  expect(safeHref('HTTP://a')).toBe('HTTP://a')
  expect(safeHref('javascript:alert(1)')).toBeNull()
  expect(safeHref('data:text/html,x')).toBeNull()
  expect(safeHref(undefined)).toBeNull()
})
