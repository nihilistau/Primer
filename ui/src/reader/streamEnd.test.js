import { expect, test } from 'vitest'
import { CUT_OFF, streamEndError } from './streamEnd.js'

test('a stream that ended with done or error ended cleanly', () => {
  expect(streamEndError({ done: true, error: false })).toBe('')
  expect(streamEndError({ done: false, error: true })).toBe('')
})
test('a stream that ended with neither was cut off', () => {
  expect(streamEndError({ done: false, error: false })).toBe(CUT_OFF)
  expect(CUT_OFF).toBe('The connection closed before the page finished.')
})
