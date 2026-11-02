import { expect, test } from 'vitest'
import { badgeLabel, rewriteOptions } from './sourceMode.js'

test('badge labels', () => {
  expect(badgeLabel('storyteller', 0)).toBe('storyteller')
  expect(badgeLabel('reference', 3)).toBe('offline reference · 3 sources')
  expect(badgeLabel('reference', 1)).toBe('offline reference · 1 source')
  expect(badgeLabel('web', 2)).toBe('web · 2 sources')
  expect(badgeLabel('web', 1)).toBe('web · 1 source')
  expect(badgeLabel('', 0)).toBe('')
})
const status = { sources: {
  storyteller: { available: true, reason: '' },
  reference: { available: false, reason: "The reference server isn't running" },
  web: { available: true, reason: '' } } }
test('rewrite options carry labels and reasons', () => {
  expect(rewriteOptions(status)).toEqual([
    { mode: 'storyteller', label: 'Storyteller', disabled: false, reason: '' },
    { mode: 'reference', label: 'Offline reference', disabled: true, reason: "The reference server isn't running" },
    { mode: 'web', label: 'Web', disabled: false, reason: '' },
  ])
})
test('a missing status leaves storyteller only usable', () => {
  const o = rewriteOptions(null)
  expect(o.map(x => x.disabled)).toEqual([false, true, true])
  expect(o[1].reason).toBeTruthy()
})
