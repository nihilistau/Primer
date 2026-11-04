import { expect, test } from 'vitest'
import { cleanSelection, selectionMenuItems, askPrefill } from './selection.js'

test('cleanSelection collapses whitespace and refuses empty or over 200 characters', () => {
  expect(cleanSelection('  a \n  castle ')).toBe('a castle')
  expect(cleanSelection('   ')).toBe('')
  expect(cleanSelection('x'.repeat(200))).toHaveLength(200)
  expect(cleanSelection('x'.repeat(201))).toBe('')
  expect(cleanSelection(null)).toBe('')
})
test('the menu offers Explore and Ask for a usable selection, nothing otherwise', () => {
  expect(selectionMenuItems('moat').map(i => i.id)).toEqual(['explore', 'ask'])
  expect(selectionMenuItems('')).toEqual([])
  expect(selectionMenuItems('y'.repeat(300))).toEqual([])
})
test('askPrefill', () => {
  expect(askPrefill(' the  moat ')).toBe('About "the moat": ')
})
