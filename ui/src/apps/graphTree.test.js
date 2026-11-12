import { describe, expect, test } from 'vitest'
import { buildForest } from './graphTree.js'
import { VIA } from './Graph.jsx'

const snap = { nodes: [{ id: 't', title: 'Trebuchet' }, { id: 'c', title: 'Counterweight' }, { id: 'l', title: 'Lift' }],
  edges: [{ from_id: null, to_id: 't', via: 'start' }, { from_id: 't', to_id: 'c', via: 'general', phrase: 'counterweight' },
          { from_id: null, to_id: 'l', via: 'typed' }, { from_id: 'l', to_id: 'c', via: 'general' }, { from_id: 'c', to_id: 't', via: 'sideways' }] }

describe('buildForest', () => {
  test('shared node appears once with alsoFrom; cycles cut', () => {
    const f = buildForest(snap)
    expect(f.map(r => r.node.id)).toEqual(['t', 'l'])
    expect(f[0].children[0].node.id).toBe('c'); expect(f[0].children[0].alsoFrom).toEqual(['Lift'])
    expect(f[0].children[0].children).toEqual([])
  })

  test('carries via and phrase of the first incoming edge', () => {
    const f = buildForest(snap)
    expect(f[0].via).toBe('start')
    expect(f[0].children[0]).toMatchObject({ via: 'general', phrase: 'counterweight' })
  })

  test('repeated edges do not duplicate children or alsoFrom', () => {
    const f = buildForest({ nodes: [{ id: 'a', title: 'A' }, { id: 'b', title: 'B' }],
      edges: [{ from_id: null, to_id: 'a', via: 'start' }, { from_id: 'a', to_id: 'b', via: 'context' },
              { from_id: 'a', to_id: 'b', via: 'context' }, { from_id: 'a', to_id: 'b', via: 'general' }] })
    expect(f).toHaveLength(1)
    expect(f[0].children).toHaveLength(1)
    expect(f[0].children[0].alsoFrom).toEqual([])
    expect(f[0].children[0].via).toBe('context')
  })

  test('edges with a missing node are skipped; unreached nodes ignored', () => {
    const f = buildForest({ nodes: [{ id: 'a', title: 'A' }, { id: 'z', title: 'Z' }],
      edges: [{ from_id: null, to_id: 'a', via: 'start' }, { from_id: 'ghost', to_id: 'a', via: 'general' },
              { from_id: 'a', to_id: 'nope', via: 'general' }] })
    expect(f.map(r => r.node.id)).toEqual(['a'])
    expect(f[0].children).toEqual([])
    expect(f[0].alsoFrom).toEqual([])
  })

  test('empty or missing snapshot gives an empty forest', () => {
    expect(buildForest({ nodes: [], edges: [] })).toEqual([])
    expect(buildForest(null)).toEqual([])
  })
})

describe('how a page was reached', () => {
  test('a page Kairos asked for is labelled so in the trail map', () => {
    expect(VIA.kairos).toEqual({ icon: 'chat', label: 'Kairos asked' })
  })
})
