import { describe, expect, it } from 'vitest'
import { cleanBlocks, decodeEntities, parseFragment, sanitize } from './sanitize.js'

const HEX = 'a'.repeat(64)

describe('sanitize keeps the builders\' HTML', () => {
  it('keeps text tags, classes and anchors as written, and ids as data-anchor', () => {
    const html = '<p class="via">From <em>a phrase</em> in <a href="#c3">Bodiam</a><sup><a href="#c3-src-2">2</a></sup></p>'
    expect(sanitize(html)).toBe(html)
    const list = '<ol class="sources"><li id="c3-src-1" value="1"><cite>Title</cite> (web)</li></ol>'
    const anchored = '<ol class="sources"><li data-anchor="c3-src-1" value="1"><cite>Title</cite> (web)</li></ol>'
    expect(sanitize(list)).toBe(anchored)
    expect(sanitize(anchored)).toBe(anchored) // cleaning twice changes nothing
    // never a real id: a book is drawn many times in one document
    expect(sanitize('<section class="cover" id="cover"><h1 id="t">T</h1></section>')).not.toMatch(/\sid=/)
    expect(sanitize('<p id="a" data-anchor="b">x</p>')).toBe('<p data-anchor="a">x</p>')
    expect(sanitize('<p data-anchor="1bad">y</p>')).toBe('<p>y</p>')
    expect(sanitize('<h2>One<br/>Two</h2>')).toBe('<h2>One<br>Two</h2>')
    expect(sanitize('<ul><li><strong>a</strong></li></ul>')).toBe('<ul><li><strong>a</strong></li></ul>')
  })
  it('keeps Primer\'s own pictures, and only those', () => {
    const fig = `<figure class="picture"><img src="/v1/media/${HEX}.png" alt="A &amp; B"/><figcaption>A &amp; B</figcaption></figure>`
    expect(sanitize(fig)).toBe(`<figure class="picture"><img src="/v1/media/${HEX}.png" alt="A &amp; B"><figcaption>A &amp; B</figcaption></figure>`)
    for (const src of ['https://x.test/a.png', 'data:image/png;base64,AAAA', `/v1/media/${HEX}.svg`, `/v1/media/../${HEX}.png`, '//x.test/a.png'])
      expect(sanitize(`<img src="${src}" alt="x">`), src).toBe('')
  })
  it('marks http(s) links external and keeps escaped text escaped', () => {
    expect(sanitize('<a href="https://example.org/a?b=1&amp;c=2">x &lt;b&gt;</a>'))
      .toBe('<a href="https://example.org/a?b=1&amp;c=2" data-external="true" rel="noopener noreferrer">x &lt;b&gt;</a>')
  })
  it('rebuilds inline SVG through cleanSvgTree', () => {
    const svg = '<figure class="map"><svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 800 500" width="800" height="500">' +
      '<title>Map of 1 place</title><rect width="800" height="500" fill="#cde"/>' +
      '<circle cx="1" cy="2" r="7" fill="#8b1e1e"/><text x="1" y="6" text-anchor="middle">1 &amp; 2</text></svg>' +
      '<figcaption>Places</figcaption></figure>'
    expect(sanitize(svg)).toBe('<figure class="map"><svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 800 500" width="800" height="500">' +
      '<title>Map of 1 place</title><rect width="800" height="500" fill="#cde"></rect>' +
      '<circle cx="1" cy="2" r="7" fill="#8b1e1e"></circle><text x="1" y="6" text-anchor="middle">1 &amp; 2</text></svg>' +
      '<figcaption>Places</figcaption></figure>')
  })
})

describe('a map plate keeps its pins', () => {
  // path data of about `kb` kilobytes, as plate.py writes land and borders
  const pathData = (kb, seed) => {
    let d = `M${seed} ${seed}`
    for (let k = 0; d.length < kb * 1024; k++) d += `L${(k * 7 + seed) % 800}.${k % 10} ${(k * 13 + seed) % 500}.${k % 7}`
    return d + 'Z'
  }
  const paths = [['#eee', 50], ['#cde', 30], ['#eee', 20], ['#cde', 10]].map(([fill, kb], k) => [fill, pathData(kb, k)])
  const border = pathData(40, 9)
  // a plate as primer/book/plate.py draws it: the frame, land, water, borders and 300 pins
  const plate = '<figure class="map"><svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 800 500" width="800" height="500">' +
    '<title>Map of 300 places</title><rect width="800" height="500" fill="#cde"/>' +
    paths.map(([fill, d]) => `<path fill="${fill}" d="${d}"/>`).join('') +
    `<path fill="none" stroke="#999" stroke-width="0.8" stroke-linejoin="round" d="${border}"/>` +
    Array.from({ length: 300 }, (_, k) => `<circle cx="${k}" cy="${k}" r="7" fill="#8b1e1e" stroke="#fff" stroke-width="1.5"/>` +
      `<text x="${k}" y="${k + 4}" font-family="sans-serif" font-size="11" font-weight="bold" text-anchor="middle" fill="#fff">${k + 1}</text>`).join('') +
    '</svg><figcaption>Places</figcaption></figure>'
  const count = (html, tag) => (html.match(new RegExp(`<${tag}[ >]`, 'g')) || []).length
  it('the fixture is a realistic size', () => {
    expect(paths.every(([, d]) => d.length >= 10 * 1024 && d.length <= 51 * 1024)).toBe(true)
    expect(plate.length).toBeGreaterThan(150 * 1024)
  })
  it('as a plate block, its land and all 300 pins survive', () => {
    const out = sanitize(plate, { kind: 'plate' })
    expect(count(out, 'path')).toBe(5)
    for (const [, d] of paths) expect(out).toContain(` d="${d}"`)
    expect(out).toContain(` d="${border}"`)
    expect(count(out, 'circle')).toBe(300)
    expect(count(out, 'text')).toBe(300)
    expect(out).toContain('>300</text>')
    expect(out.endsWith('</svg><figcaption>Places</figcaption></figure>')).toBe(true)
    expect(cleanBlocks([{ kind: 'plate', chapter: 1, html: plate }])[0].html).toBe(out)
  })
  it('a 50 KB attribute is read whole, and what follows stays a sibling', () => {
    const d = pathData(50, 3)
    expect(d.length).toBeGreaterThan(50 * 1024)
    const svg = `<svg viewBox="0 0 800 500"><path fill="#eee" d="${d}"/><circle cx="1" cy="2" r="7"/><circle cx="3" cy="4" r="7"/></svg>`
    const root = parseFragment(svg).childNodes[0]
    expect(root.childNodes.map((n) => n.localName)).toEqual(['path', 'circle', 'circle'])
    expect(root.childNodes[0].attributes.find((a) => a.name === 'd').value).toBe(d)
    expect(root.childNodes[0].childNodes).toEqual([])
    const out = sanitize(svg, { kind: 'plate' })
    expect(out).toBe(`<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 800 500"><path fill="#eee" d="${d}"></path>` +
      '<circle cx="1" cy="2" r="7"></circle><circle cx="3" cy="4" r="7"></circle></svg>')
    // the same for a long quoted HTML attribute
    const alt = 'a'.repeat(60000)
    expect(sanitize(`<img src="/v1/media/${HEX}.png" alt="${alt}"><p>after</p>`)).toBe(`<img src="/v1/media/${HEX}.png" alt="${alt}"><p>after</p>`)
  })
  it('any other block keeps the diagram cap of 200 elements', () => {
    for (const kind of ['diagram', 'para', undefined]) {
      const out = sanitize(plate, { kind })
      expect(count(out, 'svg') + count(out, 'title') + count(out, 'rect') + count(out, 'path') + count(out, 'circle') + count(out, 'text')).toBe(200)
    }
  })
})

describe('sanitize drops what is not the builders\'', () => {
  const hostile = [
    '<script>alert(1)</script>',
    '<p onclick="alert(1)" onmouseover=alert(2)>x</p>',
    '<a href="javascript:alert(1)">x</a>',
    '<a href="JaVaScRiPt:alert(1)">x</a>',
    '<a href="javascript&#58;alert(1)">x</a>',
    '<a href=" javascript:alert(1)">x</a>',
    '<a href="java\tscript:alert(1)">x</a>',
    '<a href="data:text/html,<script>alert(1)</script>">x</a>',
    '<img src=x onerror=alert(1)>',
    '<svg onload="alert(1)"><script>alert(1)</script><a href="javascript:alert(1)"><text>x</text></a></svg>',
    '<svg><foreignObject><img src=x onerror=alert(1)></foreignObject></svg>',
    '<svg><rect fill="url(javascript:alert(1))"/><set attributeName="onmouseover" to="alert(1)"/></svg>',
    '<iframe src="javascript:alert(1)"></iframe>',
    '<style>body{background:url(javascript:alert(1))}</style>',
    '<p style="background:url(javascript:alert(1))">x</p>',
    '<object data="javascript:alert(1)"></object><embed src="javascript:alert(1)">',
    '<scr<script>ipt>alert(1)</script>',
    '<SCRIPT>alert(1)</SCRIPT>',
    '<script>if (a < b) alert("</p>")</script>',
    '<!--<script>alert(1)</script>--><p>ok</p>',
    '<![CDATA[<script>alert(1)</script>]]>',
    '<form action="javascript:alert(1)"><button>x</button></form>',
    '<p>&lt;script&gt;alert(1)&lt;/script&gt;</p>',
    '<math><mtext><script>alert(1)</script></mtext></math>',
    '<div><svg><style>@import "javascript:alert(1)"</style></svg></div>',
  ]
  it('leaves no script, handler or javascript: URL', () => {
    for (const h of hostile) {
      const out = sanitize(h)
      expect(out, h).not.toMatch(/<script/i)
      expect(out, h).not.toMatch(/<(iframe|style|object|embed|form|foreignObject|set|math)\b/i)
      expect(out, h).not.toMatch(/\son\w+\s*=/i)
      expect(out, h).not.toMatch(/javascript:/i)
      expect(out, h).not.toMatch(/style=/i)
    }
  })
  it('keeps the text of unknown wrappers but not of scripts', () => {
    expect(sanitize('<blink>hi</blink>')).toBe('hi')
    expect(sanitize('<p>a<script>evil()</script>b</p>')).toBe('<p>ab</p>')
    expect(sanitize('<a href="javascript:alert(1)">x</a>')).toBe('<a>x</a>')
    expect(sanitize('<p class="a&quot; onclick=&quot;x">y</p>')).toBe('<p>y</p>')
    expect(sanitize('<p id="1bad">y</p>')).toBe('<p>y</p>')
    expect(sanitize('a < b & c')).toBe('a &lt; b &amp; c')
  })
  it('unwraps a link inside a link, keeping its text', () => {
    expect(sanitize('<p><a href="#c1">one <a href="https://x.test/">two</a> three</a></p>'))
      .toBe('<p><a href="#c1">one two three</a></p>')
    expect(sanitize('<a href="#c1"><em><a href="#c2"><strong>x</strong></a></em></a><a href="#c3">y</a>'))
      .toBe('<a href="#c1"><em><strong>x</strong></em></a><a href="#c3">y</a>')
  })
  it('survives junk and deep nesting', () => {
    expect(sanitize(null)).toBe('')
    expect(sanitize('<p>unclosed <em>em')).toBe('<p>unclosed <em>em</em></p>')
    expect(sanitize('</div></p>x')).toBe('x')
    const deep = '<div>'.repeat(5000) + 'x' + '</div>'.repeat(5000)
    expect(sanitize(deep)).not.toContain('x')
  })
})

describe('reading', () => {
  it('decodes entities once', () => {
    const ch = (...codes) => String.fromCharCode(...codes)
    expect(decodeEntities('&amp;lt; &#60; &#x3c; &nope; &#0;')).toBe('&lt; < < &nope; ' + ch(0xfffd))
    expect(decodeEntities('&mdash;&ndash;&hellip;&lsquo;&rsquo;&ldquo;&rdquo;&nbsp;&copy;&deg;&times;&middot;&apos;&quot;'))
      .toBe(ch(0x2014, 0x2013, 0x2026, 0x2018, 0x2019, 0x201c, 0x201d, 0xa0, 0xa9, 0xb0, 0xd7, 0xb7, 0x27, 0x22))
    // an entity it cannot decode is kept as literal text, and shows as written
    expect(decodeEntities('&frobnicate; &zwj2; & amp;')).toBe('&frobnicate; &zwj2; & amp;')
    expect(sanitize('<p>a &mdash; b &bogus; c &#39;d&#39; &#61;</p>')).toBe('<p>a ' + ch(0x2014) + ' b &amp;bogus; c ' + ch(0x27) + 'd' + ch(0x27) + ' =</p>')
  })
  it('keeps SVG names in their case and HTML names lower-cased', () => {
    const f = parseFragment('<P><svg viewBox="0 0 1 1"><foreignObject/></svg></P>')
    expect(f.childNodes[0].localName).toBe('p')
    const svg = f.childNodes[0].childNodes[0]
    expect(svg.namespaceURI).toBe('http://www.w3.org/2000/svg')
    expect(svg.attributes[0].name).toBe('viewBox')
    expect(svg.childNodes[0].localName).toBe('foreignObject')
  })
  it('cleanBlocks keeps kind and chapter', () => {
    expect(cleanBlocks([{ kind: 'para', chapter: 2, html: '<p onclick="x">a</p>' }]))
      .toEqual([{ kind: 'para', chapter: 2, html: '<p>a</p>' }])
  })
})

describe('sanitize stays linear on hostile whitespace', () => {
  it('a tag followed by a long run of spaces with no `>` is quick', () => {
    const t0 = performance.now()
    sanitize('<a' + ' '.repeat(100000) + '"')
    sanitize('<a b' + ' '.repeat(100000) + '"')
    expect(performance.now() - t0).toBeLessThan(500)
  })
  it('a self-closing tag with spaces before `/>` still closes', () => {
    expect(sanitize('<p>a<br   />b</p>')).toBe(sanitize('<p>a<br/>b</p>'))
  })
})
