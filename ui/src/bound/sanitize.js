/* sanitize — the bound book's client-side check on block HTML before it is inserted. The server's
 * Phase 13 builders already escape and allowlist; this is defence in depth, and the one place
 * Primer inserts HTML. The fragment is parsed here (a small XHTML reader, no DOM needed) into
 * DOM-like nodes, then written back out keeping only:
 *   - the builder tags (HTML_TAGS), each with only its own attributes;
 *   - links whose href is "#anchor" (kept) or http(s) (kept and marked data-external);
 *   - pictures whose src is Primer's own /v1/media/<64hex>.png;
 *   - inline SVG, rebuilt through diagram.js's cleanSvgTree;
 *   - an HTML element's id (cover, c1, c1-src-2...) as `data-anchor`, never as an id: a book is
 *     drawn many times over in Primer's one document (the measurer, both faces of a turning leaf,
 *     other bound windows), so real ids would repeat and clobber window.cover and the like.
 *     Links to `#c1` resolve through view.js `anchorPage`, which reads data-anchor.
 * Scripts, styles and other raw-text or embedding elements go with their content; any other
 * unknown element is unwrapped (its children are checked in turn). Comments, doctypes and
 * processing instructions are dropped. Every text and attribute value is re-escaped on output. */
import { cleanSvgTree, SVG_NS } from '../apps/diagram.js'

export const HTML_NS = 'http://www.w3.org/1999/xhtml'

export const HTML_TAGS = new Set(['p', 'h1', 'h2', 'h3', 'h4', 'em', 'strong', 'ul', 'ol', 'li', 'sup', 'a', 'br',
                                  'figure', 'img', 'figcaption', 'section', 'div', 'span', 'cite'])
const VOID = new Set(['br', 'img', 'hr', 'wbr', 'input', 'meta', 'link', 'source', 'col', 'area', 'base', 'embed',
                      'param', 'track'])
// raw-text elements: their content is read as text up to the matching end tag, never as markup
const RAW_TEXT = new Set(['script', 'style', 'textarea', 'title', 'xmp', 'iframe', 'noembed', 'noframes',
                          'noscript', 'plaintext'])
// dropped with everything inside them
const DROP = new Set([...RAW_TEXT, 'template', 'object', 'embed', 'applet', 'math', 'select', 'head', 'frameset',
                      'frame', 'form', 'button', 'input', 'video', 'audio', 'canvas', 'picture', 'source',
                      'link', 'meta', 'base', 'dialog'])
const MAX_DEPTH = 64
// a map plate draws a pin (circle and text) for up to 300 places, over its land, water and
// borders: far more than a diagram's 200 elements
export const PLATE_MAX_ELEMENTS = 1500

const ANCHOR = /^#[A-Za-z][\w-]{0,80}$/
const EXTERNAL = /^https?:\/\/[^\s"'<>\\`\x00-\x1f\x7f]+$/i
const MEDIA = /^\/v1\/media\/[0-9a-f]{64}\.png$/
const CLASS = /^[\w -]{0,120}$/
const ID = /^[A-Za-z][\w-]{0,80}$/
const NUMBER = /^-?\d{1,6}$/

/* ------------------------------------------------------------------ reading */

/* Named entities decoded; any other &name; stays as literal text. The Phase 13 builders emit
 * only &amp; &lt; &gt; &quot; and the numeric &#39; and &#61; (markdown.py `escape`), writing
 * every other character as itself; the rest are here for safety. */
const NAMED = Object.fromEntries(Object.entries({
  amp: 0x26, lt: 0x3c, gt: 0x3e, quot: 0x22, apos: 0x27, nbsp: 0xa0, mdash: 0x2014, ndash: 0x2013,
  hellip: 0x2026, lsquo: 0x2018, rsquo: 0x2019, ldquo: 0x201c, rdquo: 0x201d, copy: 0xa9,
  deg: 0xb0, times: 0xd7, middot: 0xb7, laquo: 0xab, raquo: 0xbb, shy: 0xad,
}).map(([k, v]) => [k, String.fromCharCode(v)]))
const REPLACEMENT = String.fromCharCode(0xfffd)

export function decodeEntities(s) {
  return String(s).replace(/&(#[xX][0-9a-fA-F]{1,6}|#\d{1,7}|[A-Za-z][A-Za-z0-9]{1,15});/g, (m, e) => {
    if (e[0] === '#') {
      const n = e[1] === 'x' || e[1] === 'X' ? parseInt(e.slice(2), 16) : parseInt(e.slice(1), 10)
      return n > 0 && n <= 0x10ffff && !(n >= 0xd800 && n <= 0xdfff) ? String.fromCodePoint(n) : REPLACEMENT
    }
    return Object.hasOwn(NAMED, e) ? NAMED[e] : m
  })
}

const escText = (s) => String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
const escAttr = (s) => escText(s).replace(/"/g, '&quot;')

// sticky (y) patterns, matched in place over the whole source: no window, so an attribute of
// any length (a map plate's path data runs to tens of kilobytes) is read whole
const TAG_START = /<([A-Za-z][\w:.-]*)/y
const END_TAG = /<\/([A-Za-z][\w:.-]*)\s*>/y
const ATTR = /\s*([^\s"'<>\/=]+)(?:\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s"'=<>`]+)))?/y
// whitespace before the close is skipped by hand (skipSpace): `\s*(\/?)\s*>` backtracks
// quadratically over a long run of spaces that ends without `>`
const TAG_CLOSE = /(\/?)\s*>/y
const skipSpace = (src, k) => { while (k < src.length && /\s/.test(src[k])) k++; return k }
const at = (re, src, k) => { re.lastIndex = k; return re.exec(src) }

/* Parse an (X)HTML fragment into DOM-like nodes, the shape cleanSvgTree reads: elements are
 * {nodeType: 1, localName, namespaceURI, attributes: [{name, value}], childNodes}, text is
 * {nodeType: 3, nodeValue} (decoded) and CDATA {nodeType: 4, nodeValue}. Lenient: a stray "<"
 * is text, an unmatched end tag is ignored, and open elements close at the end. Names are
 * lower-cased outside SVG; inside SVG they keep their case (viewBox, foreignObject). */
export function parseFragment(html) {
  const src = String(html == null ? '' : html)
  const root = { nodeType: 11, localName: '#fragment', namespaceURI: HTML_NS, attributes: [], childNodes: [] }
  const stack = [root]
  const top = () => stack[stack.length - 1]
  let text = ''
  const flush = () => {
    if (text) top().childNodes.push({ nodeType: 3, nodeValue: decodeEntities(text) })
    text = ''
  }
  let i = 0
  while (i < src.length) {
    const lt = src.indexOf('<', i)
    if (lt < 0) { text += src.slice(i); break }
    text += src.slice(i, lt)
    i = lt
    const rest = src.slice(i, i + 9)
    if (src.startsWith('<!--', i)) {
      const end = src.indexOf('-->', i + 4)
      flush(); i = end < 0 ? src.length : end + 3
      continue
    }
    if (rest === '<![CDATA[') {
      const end = src.indexOf(']]>', i + 9)
      flush()
      top().childNodes.push({ nodeType: 4, nodeValue: src.slice(i + 9, end < 0 ? src.length : end) })
      i = end < 0 ? src.length : end + 3
      continue
    }
    if (src[i + 1] === '!' || src[i + 1] === '?') {
      const end = src.indexOf('>', i + 2)
      flush(); i = end < 0 ? src.length : end + 1
      continue
    }
    if (src[i + 1] === '/') {
      const m = at(END_TAG, src, i)
      if (!m) { text += '<'; i++; continue }
      flush()
      const name = m[1]
      for (let k = stack.length - 1; k > 0; k--) {
        const n = stack[k]
        if (n.localName === name || n.localName.toLowerCase() === name.toLowerCase()) { stack.length = k; break }
      }
      i += m[0].length
      continue
    }
    const m = at(TAG_START, src, i)
    if (!m) { text += '<'; i++; continue }
    flush()
    const parent = top()
    const inSvg = parent.namespaceURI === SVG_NS
    let name = m[1]
    if (!inSvg) name = name.toLowerCase()
    const ns = inSvg || name === 'svg' ? SVG_NS : HTML_NS
    let j = i + m[0].length
    const attributes = []
    const seen = new Set()
    for (;;) {
      const a = at(ATTR, src, j)
      if (!a || !a[0]) break
      const an = ns === SVG_NS ? a[1] : a[1].toLowerCase()
      if (!seen.has(an)) {
        seen.add(an)
        attributes.push({ name: an, value: decodeEntities(a[2] ?? a[3] ?? a[4] ?? '') })
      }
      j += a[0].length
    }
    j = skipSpace(src, j)
    const close = at(TAG_CLOSE, src, j)
    let selfClosing = false
    if (close) { selfClosing = close[1] === '/'; j += close[0].length } else {
      const gt = src.indexOf('>', j)
      j = gt < 0 ? src.length : gt + 1
    }
    const node = { nodeType: 1, localName: name, namespaceURI: ns, attributes, childNodes: [] }
    parent.childNodes.push(node)
    i = j
    const lname = name.toLowerCase()
    // (in SVG only script and style: an SVG <title> is ordinary, allowlisted markup)
    if (!selfClosing && (lname === 'script' || lname === 'style' || (ns === HTML_NS && RAW_TEXT.has(lname)))) {
      // read up to the matching end tag as text; it is dropped anyway, but must not be parsed
      const re = new RegExp(`</${lname}\\s*>`, 'i')
      const found = re.exec(src.slice(i))
      const end = found ? i + found.index : src.length
      node.childNodes.push({ nodeType: 3, nodeValue: src.slice(i, end) })
      i = found ? end + found[0].length : src.length
      continue
    }
    if (!selfClosing && !(ns === HTML_NS && VOID.has(lname))) stack.push(node)
  }
  flush()
  return root
}

/* ------------------------------------------------------------------ writing */

/* the attributes `tag` may keep, as [name, value] pairs, in the order given */
function htmlAttributes(tag, attributes) {
  const out = []
  for (const { name, value } of attributes) {
    const v = String(value)
    if (name === 'class') { if (CLASS.test(v)) out.push(['class', v.trim().replace(/\s+/g, ' ')]) }
    else if (name === 'id' || name === 'data-anchor') { if (ID.test(v) && !out.some(([k]) => k === 'data-anchor')) out.push(['data-anchor', v]) }
    else if (tag === 'a' && name === 'href') {
      if (ANCHOR.test(v)) out.push(['href', v])
      else if (EXTERNAL.test(v)) out.push(['href', v], ['data-external', 'true'], ['rel', 'noopener noreferrer'])
    } else if (tag === 'img' && name === 'src') { if (MEDIA.test(v)) out.push(['src', v]) }
    else if (tag === 'img' && name === 'alt') out.push(['alt', v])
    else if ((tag === 'li' && name === 'value') || (tag === 'ol' && name === 'start')) { if (NUMBER.test(v)) out.push([name, v]) }
  }
  return out
}

/* a document for cleanSvgTree that builds plain records, written out by writeSvg */
const svgDoc = {
  createElementNS: (ns, name) => ({ name, attrs: [], kids: [],
    setAttribute(k, v) { this.attrs.push([k, v]) }, appendChild(c) { this.kids.push(c); return c } }),
  createTextNode: (s) => ({ text: String(s) }),
}

function writeSvg(n, root = false) {
  if (!n.name) return escText(n.text)
  const attrs = (root ? [['xmlns', SVG_NS], ...n.attrs] : n.attrs).map(([k, v]) => ` ${k}="${escAttr(v)}"`).join('')
  return `<${n.name}${attrs}>${n.kids.map((k) => writeSvg(k)).join('')}</${n.name}>`
}

/* inLink: inside an <a>, where a nested <a> is unwrapped (its text kept) */
function writeNodes(nodes, depth, svgLimit, inLink = false) {
  let out = ''
  for (const n of nodes) {
    if (n.nodeType === 3 || n.nodeType === 4) { out += escText(n.nodeValue); continue }
    if (n.nodeType !== 1) continue
    if (n.namespaceURI === SVG_NS) {
      // an <svg> is rebuilt by the diagram allowlist; anything else in that namespace is not shown
      const clean = n.localName === 'svg' ? cleanSvgTree(n, svgDoc, svgLimit) : null
      if (clean) out += writeSvg(clean, true)
      continue
    }
    const tag = n.localName
    if (DROP.has(tag) || depth >= MAX_DEPTH) continue
    if (!HTML_TAGS.has(tag) || (tag === 'a' && inLink)) { out += writeNodes(n.childNodes, depth + 1, svgLimit, inLink); continue }
    if (tag === 'img') {
      const attrs = htmlAttributes(tag, n.attributes)
      if (!attrs.some(([k]) => k === 'src')) continue // a picture from anywhere else is not shown
      out += `<img${attrs.map(([k, v]) => ` ${k}="${escAttr(v)}"`).join('')}>`
      continue
    }
    const attrs = htmlAttributes(tag, n.attributes).map(([k, v]) => ` ${k}="${escAttr(v)}"`).join('')
    if (tag === 'br') { out += '<br>'; continue }
    out += `<${tag}${attrs}>${writeNodes(n.childNodes, depth + 1, svgLimit, inLink || tag === 'a')}</${tag}>`
  }
  return out
}

/* Block HTML made safe to insert: only the builder tags and attributes, checked links and
 * pictures, and SVG rebuilt by cleanSvgTree. Anything else is dropped. `kind` is the block's
 * kind: a plate's SVG may hold up to PLATE_MAX_ELEMENTS elements, any other the diagram cap. */
export function sanitize(html, { kind } = {}) {
  const svgLimit = kind === 'plate' ? { maxElements: PLATE_MAX_ELEMENTS } : {}
  return writeNodes(parseFragment(html).childNodes, 0, svgLimit)
}

/* the blocks with their HTML sanitized, ready for layout's measurer and for insertion */
export const cleanBlocks = (blocks) => (blocks || []).map((b) => ({ ...b, html: sanitize(b && b.html, { kind: b && b.kind }) }))
