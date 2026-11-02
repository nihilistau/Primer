import { describe, expect, it } from 'vitest'
import { ATTRIBUTION, buildStyle } from './mapStyle.js'

const origin = 'http://127.0.0.1:8810'

describe('buildStyle', () => {
  const s = buildStyle(origin, { tiles: [origin + '/v1/map/tiles/{z}/{x}/{y}.mvt'], minzoom: 0, maxzoom: 9 })

  it('builds absolute asset and tile URLs from the origin', () => {
    expect(s.version).toBe(8)
    expect(s.glyphs).toBe(origin + '/v1/map/assets/fonts/{fontstack}/{range}.pbf')
    expect(s.sprite).toBe(origin + '/v1/map/assets/sprites/v4/dark')
    expect(s.sources.protomaps).toMatchObject({
      type: 'vector', tiles: [origin + '/v1/map/tiles/{z}/{x}/{y}.mvt'], minzoom: 0, maxzoom: 9 })
    expect(s.sources.protomaps.url).toBeUndefined()
  })

  it('uses the page origin for tiles even when the tilejson names another host', () => {
    const cross = buildStyle(origin, { tiles: ['http://127.0.0.1:8810/x/{z}/{x}/{y}.mvt'], minzoom: 2, maxzoom: 7 })
    const dev = buildStyle('http://localhost:5173', { tiles: ['http://127.0.0.1:8810/x'], minzoom: 2, maxzoom: 7 })
    expect(cross.sources.protomaps.tiles).toEqual([origin + '/v1/map/tiles/{z}/{x}/{y}.mvt'])
    expect(dev.sources.protomaps.tiles).toEqual(['http://localhost:5173/v1/map/tiles/{z}/{x}/{y}.mvt'])
    expect(dev.sources.protomaps).toMatchObject({ minzoom: 2, maxzoom: 7 })
  })

  it('falls back to zoom 0..14 without a tilejson', () => {
    expect(buildStyle(origin, null).sources.protomaps).toMatchObject({ minzoom: 0, maxzoom: 14 })
    expect(buildStyle(origin, {}).sources.protomaps).toMatchObject({ minzoom: 0, maxzoom: 14 })
  })

  it('always carries the attribution', () => {
    expect(ATTRIBUTION).toBe('© OpenStreetMap contributors · Protomaps')
    expect(s.sources.protomaps.attribution).toBe(ATTRIBUTION)
  })

  it('has the dark flavour layers, all drawing from the protomaps source', () => {
    expect(s.layers.length).toBeGreaterThan(20)
    const withSource = s.layers.filter(l => l.source)
    expect(withSource.length).toBeGreaterThan(20)
    expect(withSource.every(l => l.source === 'protomaps')).toBe(true)
    expect(s.layers[0].type).toBe('background')
  })

  it('does not depend on a trailing slash in the origin', () => {
    expect(buildStyle(origin + '/', null).glyphs.startsWith(origin + '/v1/')).toBe(true)
  })
})
