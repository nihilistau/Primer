import { describe, expect, test } from 'vitest'
import { THINKING_OPTIONS, gpuBackgroundNote, thinkingNow, thinkingSetting, thinkingStats } from './thinkingAhead.js'

describe('thinkingAhead', () => {
  test('options are Off / One page / Two pages', () => {
    expect(THINKING_OPTIONS.map(o => [o.value, o.label])).toEqual([[0, 'Off'], [1, 'One page'], [2, 'Two pages']])
  })
  test('setting defaults to 0 for anything unexpected', () => {
    expect(thinkingSetting({ thinking_ahead: { setting: 2 } })).toBe(2)
    expect(thinkingSetting({ thinking_ahead: { setting: 1 } })).toBe(1)
    expect(thinkingSetting({})).toBe(0)
    expect(thinkingSetting(null)).toBe(0)
    expect(thinkingSetting({ thinking_ahead: { setting: 7 } })).toBe(0)
  })
  test('stats line', () => {
    expect(thinkingStats({ written: 5, used: 2, hit_rate: 0.4 })).toBe('5 written · 2 used · hit rate 40%')
    expect(thinkingStats({ written: 3, used: 0, hit_rate: 0 })).toBe('3 written · 0 used · hit rate 0%')
    expect(thinkingStats({ written: 0, used: 0, hit_rate: null })).toBe('')
    expect(thinkingStats(undefined)).toBe('')
  })
  test('drafting line', () => {
    expect(thinkingNow({ drafting: 'Counterweight' })).toBe('drafting: Counterweight')
    expect(thinkingNow({ drafting: null })).toBe('')
  })
  test('gpu note', () => {
    expect(gpuBackgroundNote({ background: true })).toBe('thinking ahead')
    expect(gpuBackgroundNote({ background: false })).toBe('')
    expect(gpuBackgroundNote(undefined)).toBe('')
  })
})
