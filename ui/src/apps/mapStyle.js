/* mapStyle — the MapLibre style for the Map window: the Protomaps dark basemap, read from
 * Primer's own tile, glyph and sprite routes. Pure (no maplibre import), so it is testable. */
import { layers, namedFlavor } from '@protomaps/basemaps'

export const ATTRIBUTION = '© OpenStreetMap contributors · Protomaps'

/* origin is where Primer answers (window.location.origin); tilejson is /v1/map/tilejson's reply
 * (or null). The tile URL is built from the page's own origin, not from the tilejson, whose
 * Host-based URL points cross-site when the page is served by the Vite dev server. Only its
 * zoom range is used (0..14 without it). */
export function buildStyle(origin, tilejson) {
  const base = String(origin || '').replace(/\/+$/, '')
  const source = 'protomaps'
  const zoom = (v, d) => (Number.isFinite(v) ? v : d)
  return {
    version: 8,
    glyphs: base + '/v1/map/assets/fonts/{fontstack}/{range}.pbf',
    sprite: base + '/v1/map/assets/sprites/v4/dark',
    sources: {
      [source]: {
        type: 'vector', tiles: [base + '/v1/map/tiles/{z}/{x}/{y}.mvt'], attribution: ATTRIBUTION,
        minzoom: zoom(tilejson && tilejson.minzoom, 0), maxzoom: zoom(tilejson && tilejson.maxzoom, 14),
      },
    },
    layers: layers(source, namedFlavor('dark'), { lang: 'en' }),
  }
}
