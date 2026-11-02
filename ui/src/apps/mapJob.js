/* mapJob — what a map job means to the Map window: its status line, where to fit the view and
 * which concept a place opens. Pure. */
import { mapJob } from '../api.js'
import { pollJob } from './imageJob.js'

export const located = (p) => p && typeof p.lat === 'number' && typeof p.lon === 'number'
const places = (n) => `${n} ${n === 1 ? 'place' : 'places'}`

export const isEnded = (m) => !!m && (m.status === 'done' || m.status === 'failed')

export function mapStatusLabel(m) {
  if (!m) return 'Starting…'
  const list = m.places || []
  if (m.status === 'finding') return 'Finding places…'
  // the list is empty until the job is done, so the count is only known when there is one
  if (m.status === 'locating') return list.length ? `Locating ${places(list.length)}…` : 'Locating places…'
  if (m.status === 'failed') return `Failed: ${m.error || 'unknown error'}`
  if (m.status === 'done') {
    if (!list.length) return 'No places found'
    const lost = list.filter(p => !located(p)).length
    return lost ? `${places(list.length)} · ${lost} not found` : places(list.length)
  }
  return m.status || ''
}

/* [[west, south], [east, north]] around the located places; null when none. One place gets a
 * box of about a degree, since a zero-size box has no zoom. */
export function fitBounds(list) {
  const pts = (list || []).filter(located)
  if (!pts.length) return null
  let w = Math.min(...pts.map(p => p.lon)), e = Math.max(...pts.map(p => p.lon))
  let s = Math.min(...pts.map(p => p.lat)), n = Math.max(...pts.map(p => p.lat))
  // pins on both sides of the antimeridian: shift the western ones east when that box is narrower
  if (e - w > 180) {
    const lons = pts.map(p => (p.lon < 0 ? p.lon + 360 : p.lon))
    const w2 = Math.min(...lons), e2 = Math.max(...lons)
    if (e2 - w2 < e - w) { w = w2; e = e2 }
  }
  if (pts.length === 1) {
    // a ~1 degree box; at the edge of the world it slides inward rather than shrinking
    w = Math.min(Math.max(w - 0.5, -180), 179); e = w + 1
    s = Math.min(Math.max(s - 0.5, -90), 89); n = s + 1
  }
  return [[w, s], [e, n]]
}

export const placeConcept = (p) => (p && p.title) || (p && p.name) || ''

/* poll map `id` every second to its end (see imageJob.pollJob) */
export const pollMap = (id, onUpdate, opts = {}) => pollJob(id, onUpdate, { fetchJob: mapJob, ...opts })
