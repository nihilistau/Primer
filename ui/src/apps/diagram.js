/* diagram — the Diagram window's safety and words. The server rebuilds every drawing from an
 * allowlist; this is defence in depth: the SVG string is parsed (DOMParser, image/svg+xml), never
 * inserted as markup, and walked here into fresh elements, keeping only the allowlisted SVG
 * elements, each with only its own attributes (as the server allows them), and no outside reference: a
 * marker-* or <use> reference is kept only when it names, by #id, something in this same drawing
 * (a marker for marker-*; for a use, an element that is not a use and holds none), never from
 * inside a marker. The walk reads a parsed tree and builds into a document, so it is testable
 * with a stand-in for either. */
import { diagramJob } from '../api.js'
import { pollJob } from './imageJob.js'

export const SVG_NS = 'http://www.w3.org/2000/svg'
export const ALLOWED_ELEMENTS = new Set(['svg', 'g', 'line', 'polyline', 'polygon', 'rect', 'circle', 'ellipse',
                                         'path', 'text', 'tspan', 'title', 'defs', 'marker', 'use'])
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
  use: ['href', 'x', 'y', 'width', 'height', ...PRESENTATION],
}
const ATTR_SETS = Object.fromEntries(Object.entries(ALLOWED_ATTRIBUTES).map(([k, v]) => [k, new Set(v)]))
// the server writes every id as dg<up to 6 hex>-<id> (svg.py ID_PREFIX / the per-diagram prefix)
const ID_FORM = /^dg[0-9a-f]{0,6}-[A-Za-z][\w-]{0,30}$/
const MARKER_REF = /^url\(#(dg[0-9a-f]{0,6}-[A-Za-z][\w-]{0,30})\)$/
const USE_REF = /^#(dg[0-9a-f]{0,6}-[A-Za-z][\w-]{0,30})$/
// a use's reference may be written href or xlink:href (the server writes href)
const isHref = (name) => name === 'href' || name === 'xlink:href'

/* Whether `element` may keep attribute `name` with `value`: the name is on that element's list
 * (or an id of the server's form); no value holds a backslash (a CSS escape such as "\75 rl(")
 * or javascript:; url() appears only as url(#id) of the server's form, and only on marker-*; a
 * use's href (or xlink:href) is only #id of the server's form. Whether a reference resolves in
 * the drawing is cleanSvgTree's check. */
export function allowedAttribute(element, name, value) {
  if (typeof name !== 'string' || !name || !Object.hasOwn(ATTR_SETS, element)) return false
  const v = String(value == null ? '' : value)
  if (v.includes('\\') || /javascript\s*:/i.test(v)) return false
  if (name === 'id') return ID_FORM.test(v)
  if (element === 'use' && isHref(name)) return USE_REF.test(v.trim())
  if (!ATTR_SETS[element].has(name)) return false
  if (MARKERS.includes(name)) return MARKER_REF.test(v.trim())
  return !/url\s*\(/i.test(v)
}

// every rebuild gets its own id prefix (u<n>-): inline SVG ids are document-wide, and two books
// or windows may show drawings with the same server ids (each book numbers its chapters dg1-...)
let rebuilds = 0
const PREFIX_FORM = /^[A-Za-z][\w]{0,15}-$/

/* Rebuild the parsed `root` in `doc` (createElementNS, createTextNode); null unless the root is
 * an SVG-namespace <svg>. Every kept id, and every reference to one, is written with `idPrefix`
 * (by default a fresh `u<n>-` for each call), so ids are unique per rebuild: drawings from
 * different books or windows never collide. Copies of one rebuilt drawing (the bound book puts
 * the same cleaned HTML on a page, a turning leaf and its measurer) repeat its ids, and a use or
 * marker resolves to the first copy, which is identical. A given prefix must be letters, digits
 * and _ then '-' (else it throws). Text and CDATA become text nodes; comments, processing instructions and
 * anything not allowlisted (with its subtree) are dropped; depth and element count are capped.
 * `maxElements` raises or lowers the element cap (a book's map plate has up to 300 pins). */
export function cleanSvgTree(root, doc, { maxElements = MAX_ELEMENTS, idPrefix } = {}) {
  if (idPrefix !== undefined && !(typeof idPrefix === 'string' && PREFIX_FORM.test(idPrefix))) throw new Error('bad id prefix')
  if (!root || root.nodeType !== 1 || root.localName !== 'svg' || root.namespaceURI !== SVG_NS) return null
  const prefix = idPrefix === undefined ? `u${++rebuilds}-` : idPrefix
  let count = 0
  const ids = new Map()   // id -> its record (the first with that id)
  const uses = []
  // 1. the allowlisted tree as records: {name, attrs: [[k, v]], kids: [record | string], inMarker}
  const walk = (node, depth, inMarker) => {
    if (count >= maxElements || depth > MAX_DEPTH) return null
    const name = node.localName
    if (node.namespaceURI !== SVG_NS || !allowedElement(name)) return null
    if (inMarker && (name === 'marker' || name === 'use')) return null   // no nested markers, nothing re-used in one
    let ref = null
    if (name === 'use') {
      // every reference it has is the same #id of the server's form, or the use goes
      const refs = new Set()
      for (const a of Array.from(node.attributes || [])) {
        if (isHref(a.name)) refs.add(allowedAttribute('use', a.name, a.value) ? String(a.value).trim() : null)
      }
      if (refs.size !== 1 || refs.has(null)) return null
      ref = [...refs][0]
    }
    count++
    const rec = { name, attrs: [], kids: [], inMarker }
    for (const a of Array.from(node.attributes || [])) {
      if (isHref(a.name)) continue
      if (inMarker && MARKERS.includes(a.name)) continue                  // no references from inside a marker
      if (a.name === 'id' && ids.has(String(a.value))) continue            // ids stay unique
      if (allowedAttribute(name, a.name, a.value)) {
        rec.attrs.push([a.name, String(a.value)])
        if (a.name === 'id') ids.set(String(a.value), rec)
      }
    }
    if (name === 'use') { rec.attrs.unshift(['href', ref]); uses.push(rec); return rec }   // nothing inside a use
    for (const c of Array.from(node.childNodes || [])) {
      if (c.nodeType === 3 || c.nodeType === 4) rec.kids.push(String(c.nodeValue || ''))
      else if (c.nodeType === 1) { const k = walk(c, depth + 1, inMarker || name === 'marker'); if (k) rec.kids.push(k) }
    }
    return rec
  }
  const top = walk(root, 0, false)
  // 2. references must resolve in this drawing: a marker-* names a marker; a use names an
  // element that is not a use and holds none (no chains, so no cycles or exponential re-use)
  const holdsUse = (r) => r.name === 'use' || r.kids.some((k) => typeof k !== 'string' && holdsUse(k))
  const dropped = new Set()
  for (const u of uses) {
    const target = ids.get(u.attrs[0][1].slice(1))
    if (!target || holdsUse(target)) dropped.add(u)
  }
  const resolve = (r) => {
    r.attrs = r.attrs.filter(([k, v]) => {
      if (!MARKERS.includes(k)) return true
      const target = ids.get(MARKER_REF.exec(v.trim())[1])
      return !!target && target.name === 'marker'
    })
    r.kids = r.kids.filter((k) => typeof k === 'string' || !dropped.has(k))
    r.kids.forEach((k) => { if (typeof k !== 'string') resolve(k) })
  }
  resolve(top)
  // 3. built in `doc`, every id and reference with this rebuild's prefix
  const own = (k, v) => {
    if (k === 'id') return prefix + v
    if (k === 'href') return '#' + prefix + v.slice(1)
    if (MARKERS.includes(k)) return `url(#${prefix}${MARKER_REF.exec(v.trim())[1]})`
    return v
  }
  const build = (r) => {
    const out = doc.createElementNS(SVG_NS, r.name)
    for (const [k, v] of r.attrs) out.setAttribute(k, own(k, v))
    for (const k of r.kids) out.appendChild(typeof k === 'string' ? doc.createTextNode(k) : build(k))
    return out
  }
  return build(top)
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
