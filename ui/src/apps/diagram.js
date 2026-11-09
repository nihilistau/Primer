/* diagram — the Diagram window's safety and words. The server rebuilds every drawing from an
 * allowlist; this is defence in depth: the SVG string is parsed (DOMParser, image/svg+xml), never
 * inserted as markup, and walked here into fresh elements, keeping only the allowlisted SVG
 * elements, each with only its own attributes (as the server allows them), and no outside reference. The walk reads
 * a parsed tree and builds into a document, so it is testable with a stand-in for either. */
import { diagramJob } from '../api.js'
import { pollJob } from './imageJob.js'

export const SVG_NS = 'http://www.w3.org/2000/svg'
export const ALLOWED_ELEMENTS = new Set(['svg', 'g', 'line', 'polyline', 'polygon', 'rect', 'circle', 'ellipse',
                                         'path', 'text', 'tspan', 'title', 'defs', 'marker'])
const MAX_DEPTH = 40
const MAX_ELEMENTS = 200
const MAX_LABEL = 60

/* SVG names are case-sensitive: "Text" or "SCRIPT" is not an allowed element */
export const allowedElement = (name) => typeof name === 'string' && ALLOWED_ELEMENTS.has(name)

/* Each element's attributes, mirroring primer/diagrams/svg.py's ALLOWED (any of them may also
 * carry an id of the server's prefixed form). Names are exact: no class, style, handler, link,
 * tabindex or anything else unlisted. */
const PRESENTATION = ['fill', 'stroke', 'stroke-width', 'stroke-dasharray', 'stroke-linecap',
                      'stroke-linejoin', 'opacity', 'fill-opacity', 'stroke-opacity', 'transform']
const FONT = ['font-size', 'font-weight', 'font-family', 'text-anchor', 'dominant-baseline']
const MARKERS = ['marker-start', 'marker-mid', 'marker-end']
const SHAPE = [...PRESENTATION, ...MARKERS]
export const ALLOWED_ATTRIBUTES = {
  svg: ['viewBox', 'width', 'height'],
  g: [...SHAPE, ...FONT],
  line: ['x1', 'y1', 'x2', 'y2', ...SHAPE],
  polyline: ['points', ...SHAPE],
  polygon: ['points', ...SHAPE],
  rect: ['x', 'y', 'width', 'height', 'rx', 'ry', ...PRESENTATION],
  circle: ['cx', 'cy', 'r', ...PRESENTATION],
  ellipse: ['cx', 'cy', 'rx', 'ry', ...PRESENTATION],
  path: ['d', ...SHAPE],
  text: ['x', 'y', ...PRESENTATION, ...FONT],
  tspan: ['x', 'y', 'dx', 'dy', ...PRESENTATION, ...FONT],
  title: [],
  defs: [],
  marker: ['markerWidth', 'markerHeight', 'refX', 'refY', 'orient', 'markerUnits', 'viewBox', ...PRESENTATION],
}
const ATTR_SETS = Object.fromEntries(Object.entries(ALLOWED_ATTRIBUTES).map(([k, v]) => [k, new Set(v)]))
// the server writes every id as dg<up to 6 hex>-<id> (svg.py ID_PREFIX / the per-diagram prefix)
const ID_FORM = /^dg[0-9a-f]{0,6}-[A-Za-z][\w-]{0,30}$/
const MARKER_REF = /^url\(#(dg[0-9a-f]{0,6}-[A-Za-z][\w-]{0,30})\)$/

/* Whether `element` may keep attribute `name` with `value`: the name is on that element's list
 * (or an id of the server's form); no value holds a backslash (a CSS escape such as "\75 rl(")
 * or javascript:; url() appears only as url(#id) of the server's form, and only on marker-*. */
export function allowedAttribute(element, name, value) {
  if (typeof name !== 'string' || !name || !Object.hasOwn(ATTR_SETS, element)) return false
  const v = String(value == null ? '' : value)
  if (v.includes('\\') || /javascript\s*:/i.test(v)) return false
  if (name === 'id') return ID_FORM.test(v)
  if (!ATTR_SETS[element].has(name)) return false
  if (MARKERS.includes(name)) return MARKER_REF.test(v.trim())
  return !/url\s*\(/i.test(v)
}

/* Rebuild the parsed `root` in `doc` (createElementNS, createTextNode); null unless the root is
 * an SVG-namespace <svg>. Text and CDATA become text nodes; comments, processing instructions and
 * anything not allowlisted (with its subtree) are dropped; depth and element count are capped. */
export function cleanSvgTree(root, doc) {
  if (!root || root.nodeType !== 1 || root.localName !== 'svg' || root.namespaceURI !== SVG_NS) return null
  let count = 0
  const walk = (node, depth) => {
    if (count >= MAX_ELEMENTS || depth > MAX_DEPTH) return null
    if (node.namespaceURI !== SVG_NS || !allowedElement(node.localName)) return null
    count++
    const out = doc.createElementNS(SVG_NS, node.localName)
    for (const a of Array.from(node.attributes || [])) {
      if (allowedAttribute(node.localName, a.name, a.value)) out.setAttribute(a.name, a.value)
    }
    for (const c of Array.from(node.childNodes || [])) {
      if (c.nodeType === 3 || c.nodeType === 4) out.appendChild(doc.createTextNode(c.nodeValue || ''))
      else if (c.nodeType === 1) { const k = walk(c, depth + 1); if (k) out.appendChild(k) }
    }
    return out
  }
  return walk(root, 0)
}

/* the <svg> of a DOMParser result, or null if it did not parse as one */
export function svgRoot(doc) {
  if (!doc || !doc.documentElement) return null
  const root = doc.documentElement
  if (root.localName !== 'svg' || root.namespaceURI !== SVG_NS) return null
  if (doc.getElementsByTagName('parsererror').length) return null
  return root
}

/* Parse the server's drawing and rebuild it in `doc` (the page's document): the <svg> element to
 * append, or null when it cannot be shown. The DOM glue around cleanSvgTree. */
export function safeSvg(svgText, doc = document, Parser = DOMParser) {
  if (typeof svgText !== 'string' || !svgText) return null
  const parsed = new Parser().parseFromString(svgText, 'image/svg+xml')
  return cleanSvgTree(svgRoot(parsed), doc)
}

/* a <text> element's words, as a label: single-spaced, trimmed, at most 60 characters */
export function textLabel(node) {
  const s = String((node && node.textContent) || '').replace(/\s+/g, ' ').trim()
  return s.slice(0, MAX_LABEL)
}

/* the server's labels list as [{label}], deduplicated case-insensitively, in order */
export function labelHits(labels) {
  const seen = new Set(), out = []
  for (const l of Array.isArray(labels) ? labels : []) {
    if (typeof l !== 'string') continue
    const label = l.replace(/\s+/g, ' ').trim().slice(0, MAX_LABEL)
    const k = label.toLowerCase()
    if (!label || seen.has(k)) continue
    seen.add(k); out.push({ label })
  }
  return out
}

export const isEnded = (d) => !!d && (d.status === 'done' || d.status === 'failed')

export function diagramStatusLabel(d) {
  if (!d) return 'Starting…'
  if (d.status === 'queued') return 'Waiting for the writing model…'
  if (d.status === 'drawing') return 'Drawing…'
  if (d.status === 'failed') return `Failed: ${d.error || 'unknown error'}`
  if (d.status === 'done') { const n = (d.labels || []).length; return `${n} labelled ${n === 1 ? 'part' : 'parts'}` }
  return d.status || ''
}

/* poll diagram `id` every second to its end (see imageJob.pollJob) */
export const pollDiagram = (id, onUpdate, opts = {}) => pollJob(id, onUpdate, { fetchJob: diagramJob, ...opts })
