import { describe, expect, it } from 'vitest'
import { fitBounds, mapStatusLabel, placeConcept, isEnded } from './mapJob.js'

const at = (lat, lon, extra = {}) => ({ name: 'p', lat, lon, ...extra })
const none = { name: 'q', lat: null, lon: null }

describe('mapStatusLabel', () => {
  it('says what it is doing', () => {
    expect(mapStatusLabel({ status: 'finding' })).toBe('Finding places…')
    expect(mapStatusLabel({ status: 'locating', places: [at(1, 2), none, none] })).toBe('Locating 3 places…')
    expect(mapStatusLabel({ status: 'locating', places: [at(1, 2)] })).toBe('Locating 1 place…')
    expect(mapStatusLabel({ status: 'locating', places: [] })).toBe('Locating places…')
    expect(mapStatusLabel({ status: 'locating' })).toBe('Locating places…')
    expect(mapStatusLabel(null)).toBe('Starting…')
  })
  it('counts places, singular and plural, and those not found', () => {
    expect(mapStatusLabel({ status: 'done', places: [at(1, 2)] })).toBe('1 place')
    expect(mapStatusLabel({ status: 'done', places: [at(1, 2), at(3, 4)] })).toBe('2 places')
    expect(mapStatusLabel({ status: 'done', places: [at(1, 2), none] })).toBe('2 places · 1 not found')
    expect(mapStatusLabel({ status: 'done', places: [none, none] })).toBe('2 places · 2 not found')
    expect(mapStatusLabel({ status: 'done', places: [] })).toBe('No places found')
  })
  it('reports a failure with its reason', () => {
    expect(mapStatusLabel({ status: 'failed', error: 'model down' })).toBe('Failed: model down')
    expect(mapStatusLabel({ status: 'failed' })).toBe('Failed: unknown error')
  })
})

describe('isEnded', () => {
  it('is true for done and failed', () => {
    expect(isEnded({ status: 'done' })).toBe(true)
    expect(isEnded({ status: 'failed' })).toBe(true)
    expect(isEnded({ status: 'locating' })).toBe(false)
    expect(isEnded(null)).toBe(false)
  })
})

describe('fitBounds', () => {
  it('is null when nothing is located', () => {
    expect(fitBounds([])).toBeNull()
    expect(fitBounds([none])).toBeNull()
    expect(fitBounds(undefined)).toBeNull()
  })
  it('gives a ~1 degree box around a single place', () => {
    const [[w, s], [e, n]] = fitBounds([at(52, -1.5)])
    expect([w, s, e, n]).toEqual([-2, 51.5, -1, 52.5])
  })
  it('boxes several located places and ignores the rest', () => {
    expect(fitBounds([at(52, -1.5), none, at(50, 0), at(53, -3)])).toEqual([[-3, 50], [0, 53]])
  })
  it('goes the short way across the antimeridian', () => {
    expect(fitBounds([at(-17, 178), at(-21, -179)])).toEqual([[178, -21], [181, -17]])
  })
  it('clamps the single-place box to the world', () => {
    const [[w, s], [e, n]] = fitBounds([at(89.8, 179.9)])
    expect(n).toBeLessThanOrEqual(90)
    expect(e).toBeLessThanOrEqual(180)
    expect(s).toBeLessThan(n)
    expect(w).toBeLessThan(e)
  })
})

describe('placeConcept', () => {
  it('prefers the article title, else the name', () => {
    expect(placeConcept({ name: 'Warwick castle', title: 'Warwick Castle' })).toBe('Warwick Castle')
    expect(placeConcept({ name: 'Camelot', title: '' })).toBe('Camelot')
    expect(placeConcept({ name: 'Camelot', title: null })).toBe('Camelot')
  })
})
