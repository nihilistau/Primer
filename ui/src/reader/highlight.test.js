import { expect, test } from 'vitest'
import { splitByPhrases } from './highlight.js'

test('marks first occurrence only, longest first, case-insensitive', () => {
  const seen = new Set()
  const out = splitByPhrases('Castle walls and castle gates. Castle walls again.', ['castle', 'castle walls'], seen)
  expect(out.filter(x => typeof x !== 'string').map(x => x.text)).toEqual(['Castle walls', 'castle'])
})
test('word boundaries respected', () => {
  expect(splitByPhrases('cartwheel', ['cart'], new Set())).toEqual(['cartwheel'])
})
test('seen is shared across text nodes', () => {
  const seen = new Set()
  splitByPhrases('a trebuchet', ['trebuchet'], seen)
  expect(splitByPhrases('another trebuchet', ['trebuchet'], seen)).toEqual(['another trebuchet'])
})
test('regex characters in a phrase are literal', () => {
  const out = splitByPhrases('use C++ here', ['C++'], new Set())
  expect(out.some(x => typeof x !== 'string' && x.text === 'C++')).toBe(true)
})
test('no phrases leaves the text whole', () => {
  expect(splitByPhrases('plain', [], new Set())).toEqual(['plain'])
})
