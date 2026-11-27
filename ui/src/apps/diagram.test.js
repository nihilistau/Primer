import { describe, expect, it } from 'vitest'
import { ALLOWED_ELEMENTS, allowedAttribute, allowedElement, cleanSvgTree, diagramStatusLabel, labelHits,
         svgRoot, textLabel, SVG_NS } from './diagram.js'
import { parseFragment, sanitize } from '../bound/sanitize.js'
import references from '../../../tests/fixtures/svg_references.json'

/* a stand-in DOM: just what the walk reads and the builder calls */
const el = (localName, attrs = {}, children = [], ns = SVG_NS) => ({
  nodeType: 1, localName, namespaceURI: ns,
  attributes: Object.entries(attrs).map(([name, value]) => ({ name, value })),
  childNodes: children,
})
const txt = (s) => ({ nodeType: 3, nodeValue: s })
const fakeDoc = () => ({
  createElementNS: (ns, name) => ({ ns, name, attrs: {}, kids: [],
    setAttribute(k, v) { this.attrs[k] = v }, appendChild(c) { this.kids.push(c); return c } }),
  createTextNode: (s) => ({ text: s }),
})
const names = (n) => (n.name ? [n.name, ...n.kids.flatMap(names)] : [])

describe('allowlists', () => {
  it('keeps only the drawing elements', () => {
    for (const n of ['svg', 'g', 'line', 'polyline', 'polygon', 'rect', 'circle', 'ellipse', 'path', 'text', 'tspan', 'title', 'defs', 'marker'])
      expect(allowedElement(n)).toBe(true)
    for (const n of ['script', 'foreignObject', 'image', 'a', 'style', 'iframe', 'animate', 'set', 'SCRIPT', 'Text', 'svg ', '', null])
      expect(allowedElement(n)).toBe(false)
    expect(allowedElement('use')).toBe(true)
    expect(ALLOWED_ELEMENTS.size).toBe(15)
  })
  it('keeps each element\'s own attributes, as the server does', () => {
    const good = [['rect', 'x', '10'], ['rect', 'fill', '#eee'], ['line', 'x2', '3'], ['line', 'stroke-width', '2'],
                  ['line', 'marker-end', 'url(#dg1a2b3c-arrow)'], ['path', 'marker-start', 'url(#dg-arrow)'],
                  ['polyline', 'marker-mid', 'url(#dg1a2b3c-dot)'], ['text', 'font-size', '28'],
                  ['text', 'text-anchor', 'middle'], ['tspan', 'dy', '1.2'], ['g', 'transform', 'translate(1,2)'],
                  ['g', 'dominant-baseline', 'middle'], ['marker', 'refX', '10'], ['marker', 'markerWidth', '8'],
                  ['marker', 'orient', 'auto'], ['marker', 'markerUnits', 'strokeWidth'], ['marker', 'viewBox', '0 0 10 10'],
                  ['svg', 'viewBox', '0 0 1000 700'], ['circle', 'id', 'dg1a2b3c-hub'], ['marker', 'id', 'dg-arrow'],
                  ['use', 'href', '#dg-arrow'], ['use', 'xlink:href', '#dg1a2b3c-hub'], ['use', 'x', '5']]
    for (const [e, k, v] of good) expect(allowedAttribute(e, k, v), `${e} ${k}`).toBe(true)
  })
  it('drops handlers, links, styles, unlisted names and outside references', () => {
    const bad = [['svg', 'onload', 'x'], ['rect', 'ONCLICK', 'x'], ['g', 'onmouseover', ''], ['rect', 'href', '#a'],
                 ['rect', 'xlink:href', '#a'], ['rect', 'style', 'fill:red'], ['svg', 'xmlns:xlink', 'http://www.w3.org/1999/xlink'],
                 ['rect', 'class', 'x'], ['text', 'tabindex', '0'], ['text', 'autofocus', ''], ['rect', 'data-x', '1'],
                 ['rect', 'fill', 'url(#dg1a2b3c-arrow)'], ['rect', 'fill', 'url(http://x/y)'], ['rect', 'fill', '\\75 rl(http://x)'],
                 ['line', 'marker-end', 'url(http://x/y)'], ['line', 'marker-end', 'url(#arrow)'], ['line', 'marker-end', 'url("#dg-a")'],
                 ['rect', 'marker-end', 'url(#dg-arrow)'], ['text', 'x', '1\\0'], ['path', 'd', 'javascript:alert(1)'],
                 ['rect', 'x2', '1'], ['circle', 'id', 'arrow'], ['circle', 'id', 'dgxyz-arrow'], ['rect', '', 'x'], ['rect', null, 'x'],
                 ['script', 'x', '1'], ['title', 'x', '1'], ['use', 'href', '#arrow'], ['use', 'href', 'x.svg#dg-a'],
                 ['use', 'href', 'http://x/#dg-a'], ['use', 'href', 'data:image/svg+xml,#dg-a'], ['use', 'href', '#dg-a#dg-a'],
                 ['use', 'marker-end', 'url(#dg-a)'], ['line', 'xlink:href', '#dg-a']]
    for (const [e, k, v] of bad) expect(allowedAttribute(e, k, v), `${e} ${k}=${v}`).toBe(false)
  })
  it('a CSS-escaped url on fill, a class and an outside marker are dropped in the rebuild', () => {
    const root = el('svg', {}, [el('marker', { id: 'dg1a2b3c-arrow' }),
                                el('rect', { fill: '\\75 rl(http://x)', class: 'evil', x: '1' }),
                                el('line', { 'marker-end': 'url(#dg1a2b3c-arrow)' }),
                                el('line', { 'marker-end': 'url(http://x/a)' })])
    const out = cleanSvgTree(root, fakeDoc(), { idPrefix: 't-' })
    expect(out.kids[1].attrs).toEqual({ x: '1' })
    expect(out.kids[2].attrs).toEqual({ 'marker-end': 'url(#t-dg1a2b3c-arrow)' })
    expect(out.kids[3].attrs).toEqual({})
  })
})

/* the same cases as the server's cleaner (tests/test_diagrams_svg.py), on the server's ids */
describe('references stay inside the drawing', () => {
  const walk = (n, f) => { if (n && n.name) { f(n); n.kids.forEach((k) => walk(k, f)) } }
  for (const c of references.cases) {
    it(c.name, () => {
      const svg = '<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink">' +
                  c.svg.replaceAll('{p}', 'dg-') + '</svg>'
      const out = cleanSvgTree(parseFragment(svg).childNodes[0], fakeDoc(), { idPrefix: 't-' })
      const uses = [], markers = []
      walk(out, (n) => {
        if (n.name === 'use') uses.push(n.attrs.href)
        for (const [k, v] of Object.entries(n.attrs)) if (k.startsWith('marker-')) markers.push(`${k}=${v.slice('url(#t-dg-'.length, -1)}`)
        expect(n.attrs).not.toHaveProperty('xlink:href')
      })
      expect(uses).toEqual(c.uses.map((i) => `#t-dg-${i}`))
      expect(markers).toEqual(c.markers)
    })
  }
  it('a use keeps nothing inside it, and never chains however deep', () => {
    const svg = '<svg xmlns="http://www.w3.org/2000/svg"><g id="dg-a"><rect width="1" height="1"/></g>' +
                '<g id="dg-b"><use href="#dg-a"/><use href="#dg-a"/></g><g id="dg-c"><use href="#dg-b"/><use href="#dg-b"/></g>' +
                '<use href="#dg-a"><rect width="9" height="9"/></use></svg>'
    const out = cleanSvgTree(parseFragment(svg).childNodes[0], fakeDoc(), { idPrefix: 't-' })
    const uses = []
    walk(out, (n) => { if (n.name === 'use') uses.push(n) })
    expect(uses.map((u) => u.attrs.href)).toEqual(['#t-dg-a', '#t-dg-a', '#t-dg-a'])
    expect(uses[2].kids).toEqual([])
  })
})

/* inline SVG ids are document-wide: two books (or windows) showing chapter 1's diagram both carry
 * dg1- ids, so every rebuild gets its own prefix and a reference resolves only in its own copy */
describe('ids are unique to each rebuilt drawing', () => {
  const block = '<figure class="diagram"><svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1000 700">' +
    '<defs><marker id="dg1-arrow"><path d="M0 0 L1 1"/></marker><g id="dg1-weight"><rect width="4" height="4"/></g></defs>' +
    '<line x1="0" y1="0" x2="9" y2="9" marker-end="url(#dg1-arrow)"/><use href="#dg1-weight" x="5"/>' +
    '<text x="1" y="1">Arm</text></svg></figure>'
  const ids = (html) => [...html.matchAll(/ id="([^"]+)"/g)].map((m) => m[1])
  const refs = (html) => [...html.matchAll(/(?:href="#|url\(#)([^")]+)/g)].map((m) => m[1])
  it('two books with the same diagram ids, inserted in one document, never share an id', () => {
    const a = sanitize(block, { kind: 'diagram' }), b = sanitize(block, { kind: 'diagram' })
    expect(ids(a).length).toBe(2)
    expect(ids(a).filter((i) => ids(b).includes(i))).toEqual([])
    for (const html of [a, b]) {
      expect(refs(html).length).toBe(2)
      for (const r of refs(html)) expect(ids(html)).toContain(r)   // each reference names its own copy
      expect(html).not.toContain('"dg1-')
    }
  })
  it('a prefix is a fresh token per rebuild unless one is given, and must look like one', () => {
    const svg = () => parseFragment('<svg xmlns="http://www.w3.org/2000/svg"><g id="dg-a"/><use href="#dg-a"/></svg>').childNodes[0]
    const one = cleanSvgTree(svg(), fakeDoc()), two = cleanSvgTree(svg(), fakeDoc())
    expect(one.kids[0].attrs.id).toMatch(/^u\d+-dg-a$/)
    expect(one.kids[0].attrs.id).not.toBe(two.kids[0].attrs.id)
    expect(one.kids[1].attrs.href).toBe('#' + one.kids[0].attrs.id)
    expect(() => cleanSvgTree(svg(), fakeDoc(), { idPrefix: '' })).toThrow()
    expect(() => cleanSvgTree(svg(), fakeDoc(), { idPrefix: '"><script>' })).toThrow()
  })
})

describe('cleanSvgTree', () => {
  it('rebuilds a good drawing', () => {
    const root = el('svg', { viewBox: '0 0 1000 700', width: '1000' }, [
      el('defs', {}, [el('marker', { id: 'dg1-arrow' }, [el('path', { d: 'M0 0L10 5L0 10z' })])]),
      el('line', { x1: '1', y1: '2', x2: '3', y2: '4', 'marker-end': 'url(#dg1-arrow)' }),
      el('text', { x: '10', y: '20' }, [txt('the arm'), el('tspan', {}, [txt(' (long)')])]),
    ])
    const out = cleanSvgTree(root, fakeDoc(), { idPrefix: 't-' })
    expect(out.name).toBe('svg')
    expect(out.ns).toBe(SVG_NS)
    expect(names(out)).toEqual(['svg', 'defs', 'marker', 'path', 'line', 'text', 'tspan'])
    expect(out.kids[1].attrs['marker-end']).toBe('url(#t-dg1-arrow)')
    expect(out.kids[2].kids[0]).toEqual({ text: 'the arm' })
  })
  it('drops scripts, foreign content and handlers with their subtrees', () => {
    const root = el('svg', { onload: 'alert(1)' }, [
      el('script', {}, [txt('alert(1)')]),
      el('foreignObject', {}, [el('div', {}, [], 'http://www.w3.org/1999/xhtml')]),
      el('g', { onclick: 'x', style: 'x', transform: 'translate(1,2)' }, [el('use', { href: '#a' }), el('rect', { 'xlink:href': 'javascript:1', x: '1' })]),
      el('text', {}, [txt('x')], 'http://www.w3.org/1999/xhtml'),
      el('a', { href: 'http://evil' }, [el('text', {}, [txt('click')])]),
    ])
    const out = cleanSvgTree(root, fakeDoc())
    expect(names(out)).toEqual(['svg', 'g', 'rect'])
    expect(out.attrs).toEqual({})
    expect(out.kids[0].attrs).toEqual({ transform: 'translate(1,2)' })
    expect(out.kids[0].kids[0].attrs).toEqual({ x: '1' })
  })
  it('keeps CDATA as text and drops comments and processing instructions', () => {
    const root = el('svg', {}, [el('text', {}, [{ nodeType: 4, nodeValue: '<b>' }, { nodeType: 8, nodeValue: 'c' }, { nodeType: 7, nodeValue: 'pi' }])])
    const out = cleanSvgTree(root, fakeDoc())
    expect(out.kids[0].kids).toEqual([{ text: '<b>' }])
  })
  it('refuses a root that is not an SVG svg', () => {
    expect(cleanSvgTree(el('html', {}, [], 'http://www.w3.org/1999/xhtml'), fakeDoc())).toBeNull()
    expect(cleanSvgTree(el('svg', {}, [], 'http://www.w3.org/1999/xhtml'), fakeDoc())).toBeNull()
    expect(cleanSvgTree(el('g'), fakeDoc())).toBeNull()
    expect(cleanSvgTree(null, fakeDoc())).toBeNull()
  })
  it('caps depth and element count', () => {
    let deep = el('rect')
    for (let i = 0; i < 100; i++) deep = el('g', {}, [deep])
    const out = cleanSvgTree(el('svg', {}, [deep]), fakeDoc())
    expect(names(out).length).toBeLessThanOrEqual(41)
    const wide = cleanSvgTree(el('svg', {}, Array.from({ length: 1000 }, () => el('rect'))), fakeDoc())
    expect(names(wide).length).toBeLessThanOrEqual(200)
  })
  it('takes a higher element cap when asked, and keeps the old one by default', () => {
    const rects = () => el('svg', {}, Array.from({ length: 1000 }, () => el('rect')))
    expect(names(cleanSvgTree(rects(), fakeDoc())).length).toBe(200)
    expect(names(cleanSvgTree(rects(), fakeDoc(), {})).length).toBe(200)
    expect(names(cleanSvgTree(rects(), fakeDoc(), { maxElements: 1500 })).length).toBe(1001)
    expect(names(cleanSvgTree(rects(), fakeDoc(), { maxElements: 10 })).length).toBe(10)
  })
})

describe('svgRoot', () => {
  it('is the svg element of a parsed document, or null on a parse error', () => {
    const svg = el('svg')
    expect(svgRoot({ documentElement: svg, getElementsByTagName: () => [] })).toBe(svg)
    expect(svgRoot({ documentElement: svg, getElementsByTagName: (n) => (n === 'parsererror' ? [{}] : []) })).toBeNull()
    expect(svgRoot({ documentElement: el('parsererror', {}, [], 'http://www.mozilla.org/newlayout/xml/parsererror.xml'), getElementsByTagName: () => [] })).toBeNull()
    expect(svgRoot(null)).toBeNull()
  })
})

describe('labels', () => {
  it('textLabel is the trimmed, single-spaced text, at most 60 characters', () => {
    expect(textLabel({ textContent: '  the   counter\nweight ' })).toBe('the counter weight')
    expect(textLabel({ textContent: 'x'.repeat(80) }).length).toBe(60)
    expect(textLabel({ textContent: '' })).toBe('')
    expect(textLabel(null)).toBe('')
  })
  it('labelHits dedupes the server labels, case-insensitively, in order', () => {
    expect(labelHits(['Arm', 'sling', 'arm', ' ', 7, null, 'Counterweight']))
      .toEqual([{ label: 'Arm' }, { label: 'sling' }, { label: 'Counterweight' }])
    expect(labelHits(null)).toEqual([])
  })
  it('diagramStatusLabel reads a job', () => {
    expect(diagramStatusLabel(null)).toBe('Starting…')
    expect(diagramStatusLabel({ status: 'queued' })).toBe('Waiting for the writing model…')
    expect(diagramStatusLabel({ status: 'drawing' })).toBe('Drawing…')
    expect(diagramStatusLabel({ status: 'done', labels: ['a', 'b', 'c'] })).toBe('3 labelled parts')
    expect(diagramStatusLabel({ status: 'failed', error: 'too few labels' })).toBe('Failed: too few labels')
  })
})
