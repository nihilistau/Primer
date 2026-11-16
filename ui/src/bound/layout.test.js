import { describe, expect, it } from 'vitest'
import { FIGURES, fitScale, isHeadingBlock, layout, layoutAll, locate, splitPara, spreadOf, spreads } from './layout.js'

/* The fake measurer: a figure's height is in its class (h-300); text is characters x line
 * height, 40 characters a line, 20 px a line. */
const LINE = 20
const PER_LINE = 40
const textOf = (html) => html.replace(/<[^>]*>/g, '').replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&amp;/g, '&')
const measure = (html) => {
  const fig = /class="[^"]*\bh-(\d+)/.exec(html)
  if (fig) return Number(fig[1])
  const n = textOf(html).length
  return n ? Math.ceil(n / PER_LINE) * LINE : 0
}
const PAGE = 400 // 20 lines of 40 characters
const opts = { measure, pageHeight: PAGE }

const words = (n, seed = 0) => Array.from({ length: n }, (_, k) => `w${seed}x${k}`).join(' ')
const para = (chapter, text) => ({ kind: 'para', chapter, html: `<p>${text}</p>` })
const lines = (chapter, n, seed = 0) => para(chapter, 'y'.repeat(PER_LINE * n - 2) + seed) // exactly n lines, one word
const heading = (chapter, t = 'Heading') => ({ kind: 'heading', chapter, html: `<h2>${t}</h2>` })
const fig = (kind, chapter, h) => ({ kind, chapter, html: `<figure class="${kind} h-${h}"><figcaption>c</figcaption></figure>` })

function book(chapters = 3, parasPer = 6) {
  const out = [{ kind: 'cover', chapter: 0, html: '<section class="cover"><h1>T</h1></section>' },
               { kind: 'contents', chapter: 0, html: '<ol><li><a href="#c1">One</a></li></ol>' }]
  for (let c = 1; c <= chapters; c++) {
    out.push({ kind: 'chapter-head', chapter: c, html: `<h1>Chapter ${c}</h1>` })
    for (let k = 0; k < parasPer; k++) {
      if (k === 2) out.push(heading(c, `Part ${c}.${k}`))
      out.push(para(c, words(15 + ((c * 7 + k * 13) % 60), c * 100 + k)))
      if (k === 3) out.push(fig('picture', c, 120 + ((c * 37) % 200)))
    }
    out.push(fig('plate', c, 180 + ((c * 53) % 150)))
    out.push({ kind: 'sources', chapter: c, html: `<ol class="sources"><li id="c${c}-src-1" value="1"><cite>S</cite></li></ol>` })
  }
  out.push({ kind: 'closing', chapter: 0, html: '<p>The end.</p>' },
           { kind: 'back', chapter: 0, html: '<p>Made with Primer</p>' })
  return out
}

/* The blocks' text, rebuilt from the pages: each block's pieces joined in order. */
function rebuilt(pages) {
  const byBlock = new Map()
  for (const p of pages) for (const it of p.items) byBlock.set(it.blockIndex, (byBlock.get(it.blockIndex) || '') + textOf(it.html))
  return byBlock
}
const items = (pages) => pages.flatMap((p, pi) => p.items.map((it) => ({ ...it, page: pi })))
const used = (p) => p.items.reduce((h, it) => h + measure(it.html), 0)

/* The book's invariants, checked on any layout:
 *   - every block appears exactly once (a figure as one item; text as its parts in order), and
 *     its text is all there;
 *   - order is kept, except that a figure may be deferred: then it lands at the top of the very
 *     next page, and only blocks of its own chapter on the page before pass it;
 *   - no page is left more than half empty by a deferred figure when there was text to fill it;
 *   - keep with next: no page ends with a whole heading while its next block starts later. */
function checkBook(blocks, pages, height = PAGE) {
  const all = items(pages)
  const text = rebuilt(pages)
  blocks.forEach((b, k) => {
    const mine = all.filter((it) => it.blockIndex === k)
    expect(mine.length, `block ${k}`).toBeGreaterThan(0)
    if (FIGURES.has(b.kind)) expect(mine).toHaveLength(1)
    else if (mine.length > 1) expect(mine.map((it) => it.part)).toEqual(mine.map((_, j) => j))
    expect(text.get(k), `block ${k}`).toBe(textOf(b.html || ''))
  })
  for (const p of pages) expect(p.items.length).toBeGreaterThan(0)
  // no overflow: a page holds no more than its height, unless it is one figure scaled to fit
  pages.forEach((p, pi) => {
    if (p.items.some((it) => it.fit)) {
      expect(p.items, `page ${pi} fit`).toHaveLength(1)
      // scaled to the page: the whole block shows, never more than the page tall
      const it = p.items[0]
      expect(it.scale, `page ${pi} scale`).toBe(fitScale(measure(it.html), height))
      expect(measure(it.html) * it.scale, `page ${pi} scaled`).toBeLessThanOrEqual(height)
    }
    else expect(used(p), `page ${pi} overflows`).toBeLessThanOrEqual(height)
  })
  // locate agrees with a plain scan
  blocks.forEach((_, k) => expect(locate(pages, k), `locate ${k}`).toBe(pages.findIndex((p) => p.items.some((it) => it.blockIndex === k))))
  expect(pages.map((p) => p.folio)).toEqual(pages.map((_, k) => k + 1))
  // text alone is in order
  const textSeq = all.filter((it) => !FIGURES.has(blocks[it.blockIndex].kind)).map((it) => it.blockIndex)
  textSeq.forEach((b, k) => k && expect(b).toBeGreaterThanOrEqual(textSeq[k - 1]))
  all.forEach((it, k) => {
    const passedBy = all.slice(0, k).filter((o) => o.blockIndex > it.blockIndex)
    if (!passedBy.length) return
    // a deferred figure: at the top of its page, passed only by its own chapter's blocks on the page before
    expect(FIGURES.has(blocks[it.blockIndex].kind), `block ${it.blockIndex} passed`).toBe(true)
    expect(pages[it.page].items[0].blockIndex).toBe(it.blockIndex)
    for (const o of passedBy) {
      expect(o.page).toBe(it.page - 1)
      expect(blocks[o.blockIndex].chapter).toBe(blocks[it.blockIndex].chapter)
      expect(blocks[o.blockIndex].kind).not.toBe('chapter-head')
    }
    // and its gap was filled: the page before is more than half full, or the text ran out
    const prev = pages[it.page - 1]
    if (used(prev) <= PAGE / 2) {
      const after = blocks[Math.max(...prev.items.map((o) => o.blockIndex)) + 1]
      const fillable = !!after && after.chapter === blocks[it.blockIndex].chapter && ['para', 'list', 'sources', 'story', 'closing'].includes(after.kind)
      expect(fillable && measure(after.html) <= PAGE - used(prev), `gap before page ${it.page}`).toBe(false)
    }
  })
  // a figure that went on whole to the next page, leaving a page under half full, had no text to fill it
  pages.forEach((p, pi) => {
    if (pi === 0 || p.kind) return
    const first = p.items[0]
    const prev = pages[pi - 1]
    if (!FIGURES.has(blocks[first.blockIndex].kind) || first.fit || prev.kind || prev.chapter !== p.chapter) return
    if (used(prev) > PAGE / 2) return
    const after = blocks[first.blockIndex + 1]
    const fillable = !!after && locate(pages, first.blockIndex + 1) !== pi - 1 && after.chapter === blocks[first.blockIndex].chapter &&
      !FIGURES.has(after.kind) && !['chapter-head', 'cover', 'back'].includes(after.kind)
    expect(fillable && measure(after.html) <= PAGE - used(prev), `page ${pi} left half empty`).toBe(false)
  })
  // keep with next
  pages.forEach((p, pi) => {
    const last = p.items.at(-1)
    if (!['heading', 'chapter-head'].includes(blocks[last.blockIndex].kind) || last.part != null) return
    if (p.items.every((it) => ['heading', 'chapter-head'].includes(blocks[it.blockIndex].kind))) return
    const nextBlock = last.blockIndex + 1
    if (nextBlock >= blocks.length || blocks[nextBlock].chapter !== blocks[last.blockIndex].chapter) return
    expect(locate(pages, nextBlock), `heading ${last.blockIndex} ends page ${pi}`).toBe(pi)
  })
}

describe('layout', () => {
  it('places every block once, in order but for deferred figures', () => {
    for (const [c, n] of [[3, 6], [5, 9], [8, 4]]) {
      const blocks = book(c, n)
      checkBook(blocks, layoutAll(blocks, opts))
    }
    for (const h of [250, 300, 380, 500]) {
      const blocks = book(4, 7)
      checkBook(blocks, layoutAll(blocks, { measure, pageHeight: h }), h)
    }
  })
  it('splits a long paragraph at a word boundary, and it rejoins', () => {
    const long = `Start <em>emphasised ${words(30, 1)}</em> and <a href="#c1-src-1">linked ${words(200, 2)}</a> end.`
    const blocks = [para(1, words(100, 3)), { kind: 'para', chapter: 1, html: `<p data-anchor="c1-p">${long}</p>` }]
    const pages = layoutAll(blocks, opts)
    checkBook(blocks, pages)
    const pieces = items(pages).filter((it) => it.blockIndex === 1)
    expect(pieces.length).toBeGreaterThan(1)
    for (const it of pieces) {
      for (const t of ['p', 'em', 'a']) {
        expect((it.html.match(new RegExp(`<${t}[ >]`, 'g')) || []).length).toBe((it.html.match(new RegExp(`</${t}>`, 'g')) || []).length)
      }
    }
    // the anchor stays where the paragraph starts; a reopened link keeps its href
    expect(pieces.filter((it) => it.html.includes('data-anchor="c1-p"'))).toHaveLength(1)
    expect(pieces[0].html).toContain('data-anchor="c1-p"')
    expect(pieces.slice(1).some((it) => it.html.startsWith('<p><a href="#c1-src-1">'))).toBe(true)
    for (const it of pieces.slice(0, -1)) expect(textOf(it.html)).toMatch(/\s$/)
  })
  it('never splits a plate, picture, timeline or diagram', () => {
    const blocks = [para(1, words(120, 1)), fig('plate', 1, 300), fig('picture', 1, 250), fig('timeline', 1, 390), fig('diagram', 1, 200)]
    const pages = layoutAll(blocks, opts)
    checkBook(blocks, pages)
    for (const k of [1, 2, 3, 4]) {
      const mine = items(pages).filter((it) => it.blockIndex === k)
      expect(mine).toHaveLength(1)
      expect(mine[0].part).toBeUndefined()
      expect(mine[0].fit).toBeUndefined()
    }
  })
  it('defers a figure that does not fit, and the following text closes the gap', () => {
    const blocks = [lines(1, 10, 1), fig('plate', 1, 220), lines(1, 5, 2), lines(1, 5, 3), lines(1, 8, 4)]
    const pages = layoutAll(blocks, opts)
    checkBook(blocks, pages)
    expect(pages[0].items.map((it) => it.blockIndex)).toEqual([0, 2, 3])
    expect(used(pages[0])).toBe(PAGE)
    expect(pages[1].items.map((it) => it.blockIndex)).toEqual([1, 4])
  })
  it('splits the filling text, and the figure heads the next page before its tail', () => {
    const blocks = [lines(1, 12, 1), fig('picture', 1, 250), para(1, words(150, 2))]
    const pages = layoutAll(blocks, opts)
    checkBook(blocks, pages)
    expect(pages[0].items.map((it) => [it.blockIndex, it.part])).toEqual([[0, undefined], [2, 0]])
    expect(pages[1].items.map((it) => [it.blockIndex, it.part])).toEqual([[1, undefined], [2, 1]])
  })
  it('a deferred figure never passes a chapter head or another figure, and moves one page at most', () => {
    const blocks = [lines(1, 15, 1), fig('plate', 1, 300), lines(1, 2, 2), fig('diagram', 1, 60), lines(1, 2, 3),
                    lines(2, 15, 4), fig('plate', 2, 300), lines(2, 2, 5), { kind: 'chapter-head', chapter: 3, html: '<h1>Three</h1>' }]
    const pages = layoutAll(blocks, opts)
    checkBook(blocks, pages)
    expect(pages.map((p) => p.items.map((it) => it.blockIndex))).toEqual([[0, 2], [1, 3, 4], [5, 7], [6], [8]])
  })
  it('a figure with no text after it simply moves on', () => {
    const blocks = [lines(1, 15, 1), fig('plate', 1, 300), fig('picture', 1, 50)]
    const pages = layoutAll(blocks, opts)
    checkBook(blocks, pages)
    expect(pages.map((p) => p.items.map((it) => it.blockIndex))).toEqual([[0], [1, 2]])
  })
  it('gives an oversize plate fit, alone on its page, with text closing its gap', () => {
    const blocks = [para(1, 'short'), fig('plate', 1, 1000), para(1, 'after')]
    const pages = layoutAll(blocks, opts)
    checkBook(blocks, pages)
    expect(pages).toHaveLength(2)
    expect(pages[0].items.map((it) => it.blockIndex)).toEqual([0, 2])
    expect(pages[1].items).toEqual([{ blockIndex: 1, html: blocks[1].html, fit: true, scale: 0.4 }])
    const alone = layoutAll([para(1, 'short'), fig('plate', 1, 1000)], opts)
    expect(alone[1].items[0].fit).toBe(true)
  })
  it('scales a timeline taller than a page to fit it, never losing an event', () => {
    const events = Array.from({ length: 8 }, (_, k) => `<li><span class="date">${1900 + k}</span> ${'long label '.repeat(12)}${k}</li>`).join('')
    const tl = { kind: 'timeline', chapter: 1, html: `<section class="timeline"><h2>Timeline</h2><ol class="timeline">${events}</ol></section>` }
    expect(measure(tl.html)).toBeGreaterThan(PAGE)
    const blocks = [para(1, 'before'), tl, para(1, 'after')]
    const pages = layoutAll(blocks, opts)
    checkBook(blocks, pages)
    const it = items(pages).find((x) => x.blockIndex === 1)
    expect(it.fit).toBe(true)
    expect(it.html).toBe(tl.html) // whole: every event is there
    expect(it.scale).toBeGreaterThan(0)
    expect(it.scale).toBeLessThan(1)
    expect(measure(tl.html) * it.scale).toBeLessThanOrEqual(PAGE)
  })
  it('fitScale shrinks just enough, and leaves a block that fits alone', () => {
    expect(fitScale(1000, 400)).toBe(0.4)
    expect(fitScale(300, 400)).toBe(1)
    expect(fitScale(400, 400)).toBe(1)
    expect(fitScale(3, 1) * 3).toBeLessThanOrEqual(1)
    expect(fitScale(NaN, 400)).toBe(1)
    expect(fitScale(1000, 0)).toBe(1)
  })
  it('keeps a heading with its next block', () => {
    const blocks = [lines(1, 19, 1), heading(1), lines(1, 5, 2)]
    const pages = layoutAll(blocks, opts)
    checkBook(blocks, pages)
    expect(pages.map((p) => p.items.map((it) => it.blockIndex))).toEqual([[0], [1, 2]])
    // and with a figure that moves on whole
    const b2 = [lines(1, 15, 1), heading(1), fig('plate', 1, 300), { kind: 'chapter-head', chapter: 2, html: '<h1>Two</h1>' }]
    const p2 = layoutAll(b2, opts)
    checkBook(b2, p2)
    expect(p2.map((p) => p.items.map((it) => it.blockIndex))).toEqual([[0], [1, 2], [3]])
    // two headings go together; a chapter head alone on its page stays
    const b3 = [{ kind: 'chapter-head', chapter: 1, html: '<h1>One</h1>' }, lines(1, 17, 1), heading(1, 'A'), heading(1, 'B'), fig('plate', 1, 390)]
    const p3 = layoutAll(b3, opts)
    checkBook(b3, p3)
    expect(p3.map((p) => p.items.map((it) => it.blockIndex))).toEqual([[0, 1], [2, 3], [4]])
    const b4 = [{ kind: 'chapter-head', chapter: 1, html: '<h1>One</h1>' }, fig('plate', 1, 395)]
    expect(layoutAll(b4, opts).map((p) => p.items.map((it) => it.blockIndex))).toEqual([[0], [1]])
  })
  it('keeps a story or scene title with its next block, and never splits it', () => {
    const story = (html) => ({ kind: 'story', chapter: 0, html })
    const blocks = [story('<p>' + 'y'.repeat(PER_LINE * 19 - 2) + '</p>'), story('<h2>The throw</h2>'), story('<p>She pulls the arm back and lets go.</p>')]
    const pages = layoutAll(blocks, opts)
    checkBook(blocks, pages)
    expect(pages.map((p) => p.items.map((it) => it.blockIndex))).toEqual([[0], [1, 2]])
    expect(isHeadingBlock(blocks[1])).toBe(true)
    expect(isHeadingBlock(blocks[0])).toBe(false)
    expect(isHeadingBlock(story('<figure class="picture"><h2>x</h2></figure>'))).toBe(false)
    // a long title is moved whole, not split, when it does not fit
    const long = story('<h3>' + words(30, 7) + '</h3>')
    const b2 = [story('<p>' + 'y'.repeat(PER_LINE * 18 - 2) + '</p>'), long, story('<p>after</p>')]
    const p2 = layoutAll(b2, opts)
    checkBook(b2, p2)
    expect(items(p2).filter((it) => it.blockIndex === 1)).toHaveLength(1)
  })
  it('a heading moved on with its text does not take a deferred figure out of order', () => {
    const blocks = [lines(1, 12, 1), heading(1), fig('picture', 1, 300), heading(1, 'B'), lines(1, 8, 2)]
    const pages = layoutAll(blocks, opts)
    checkBook(blocks, pages)
  })
  it('starts each chapter on a new page and gives pages their chapter', () => {
    const blocks = book(4, 3)
    const pages = layoutAll(blocks, opts)
    for (let k = 0; k < blocks.length; k++) {
      if (blocks[k].kind !== 'chapter-head') continue
      const at = locate(pages, k)
      expect(pages[at].items[0].blockIndex).toBe(k)
      expect(pages[at].chapter).toBe(blocks[k].chapter)
    }
    for (const p of pages) for (const it of p.items) expect(blocks[it.blockIndex].chapter).toBe(p.chapter)
  })
  it('puts the cover and the back alone on their pages', () => {
    const pages = layoutAll(book(), opts)
    expect(pages[0].kind).toBe('cover')
    expect(pages[0].items).toHaveLength(1)
    expect(pages.at(-1).kind).toBe('back')
    expect(pages.at(-1).items).toHaveLength(1)
    expect(pages.filter((p) => p.kind)).toHaveLength(2)
  })
  it('never loops on a word taller than a page, and never loses it', () => {
    const giant = 'x'.repeat(PER_LINE * 40)
    const pages = layoutAll([para(1, `a ${giant} b`), para(1, giant)], opts)
    expect(rebuilt(pages).get(0)).toBe(`a ${giant} b`)
    expect(rebuilt(pages).get(1)).toBe(giant)
    expect(items(pages).filter((it) => it.fit)).toHaveLength(2)
  })
  it('copes with bad measures and empty blocks', () => {
    const pages = layoutAll([para(1, 'a'), { kind: 'para', chapter: 1, html: '' }, null],
                            { measure: () => NaN, pageHeight: 100 })
    expect(items(pages).map((it) => it.blockIndex)).toEqual([0, 1, 2])
    expect(() => layout([], { pageHeight: 10 })).toThrow()
    expect(() => layout([], { measure, pageHeight: 0 })).toThrow()
  })
  it('continues in chunks to the same pages', () => {
    const blocks = book(5, 8)
    const whole = layoutAll(blocks, opts)
    for (const chunk of [1, 2, 3, 7]) {
      const got = []
      let r = layout(blocks, { ...opts, chunk })
      let calls = 1
      got.push(...r.pages)
      while (!r.done) {
        expect(r.next).not.toBeNull()
        r = layout(blocks, { ...opts, chunk, startAt: JSON.parse(JSON.stringify(r.next)) })
        got.push(...r.pages)
        calls++
      }
      expect(r.next).toBeNull()
      expect(calls).toBeGreaterThan(5)
      expect(got).toEqual(whole)
    }
    let stops = 0
    const y = layout(blocks, { ...opts, shouldYield: () => ++stops > 2 })
    expect(y.done).toBe(false)
    expect(y.next.i).toBe(3)
  })
  it('lays out a 1000-block book in under 500 ms', () => {
    const blocks = [{ kind: 'cover', chapter: 0, html: '<h1>T</h1>' }]
    for (let c = 1; blocks.length < 999; c++) {
      blocks.push({ kind: 'chapter-head', chapter: c, html: `<h1>C${c}</h1>` })
      for (let k = 0; k < 6; k++) {
        blocks.push(para(c, words(30 + ((c + k) % 9) * 25, c)))
        if (k === 2) blocks.push(fig('picture', c, 220))
      }
      blocks.push(fig('plate', c, 260))
    }
    blocks.length = 999
    blocks.push({ kind: 'back', chapter: 0, html: '<p>Made with Primer</p>' })
    const t0 = performance.now()
    const pages = layoutAll(blocks, opts)
    const ms = performance.now() - t0
    expect(blocks).toHaveLength(1000)
    expect(ms).toBeLessThan(500)
    checkBook(blocks, pages)
  })
})

describe('splitPara', () => {
  const NBSP = String.fromCharCode(0xa0)
  it('never breaks at a no-break space', () => {
    const html = `<p>one two${NBSP}three four</p>`
    // "one two" + NBSP + "three" is one unbreakable run
    expect(splitPara(html, (h) => textOf(h).length <= 12)).toEqual(['<p>one </p>', `<p>two${NBSP}three four</p>`])
    expect(splitPara(html, (h) => textOf(h).length <= 14)).toEqual([`<p>one two${NBSP}three </p>`, '<p>four</p>'])
    expect(splitPara(`<p>a${NBSP}b${NBSP}c</p>`, () => true)).toEqual(['', `<p>a${NBSP}b${NBSP}c</p>`])
  })
  it('never cuts a head of only whitespace', () => {
    // leading whitespace, and a block start right after it
    expect(splitPara('<p>   alpha beta</p>', (h) => textOf(h).length <= 4)).toEqual(['', '<p>   alpha beta</p>'])
    expect(splitPara('<div> <p>alpha</p><p>beta</p></div>', (h) => textOf(h).length <= 2)).toEqual(['', '<div> <p>alpha</p><p>beta</p></div>'])
    expect(splitPara('<div> <p>alpha</p><p>beta</p></div>', (h) => textOf(h).length <= 6)).toEqual(['<div> <p>alpha</p></div>', '<div><p>beta</p></div>'])
    // a head holding a picture is not blank
    expect(splitPara('<p><img src="/x.png" alt=""> alpha</p>', (h) => textOf(h).length <= 1)).toEqual(['<p><img src="/x.png" alt=""> </p>', '<p>alpha</p>'])
    expect(splitPara('<p> a b c</p>', (h) => textOf(h).length <= 3)).toEqual(['<p> a </p>', '<p>b c</p>'])
  })
  it('splits at the last boundary that fits', () => {
    const [head, tail] = splitPara('<p>one two <em>three four</em> five</p>', (h) => textOf(h).length <= 16)
    expect(head).toBe('<p>one two <em>three </em></p>')
    expect(tail).toBe('<p><em>four</em> five</p>')
  })
  it('gives no head when the first word does not fit, and the html back', () => {
    expect(splitPara('<p>alpha beta</p>', () => false)).toEqual(['', '<p>alpha beta</p>'])
    expect(splitPara('<p>alpha</p>', () => true)).toEqual(['', '<p>alpha</p>'])
  })
  it('does not cut inside a picture or an svg', () => {
    const html = '<p>a <img src="/x.png" alt="pic"> b <svg viewBox="0 0 1 1"><text>t u v</text></svg> c</p>'
    const [head, tail] = splitPara(html, (h) => !h.includes('<svg'))
    expect(head).toBe('<p>a <img src="/x.png" alt="pic"> b </p>')
    expect(tail).toBe('<p><svg viewBox="0 0 1 1"><text>t u v</text></svg> c</p>')
  })
  it('splits a list between items, and a continued <ol> numbers on', () => {
    const ol = '<ol><li>alpha</li><li>beta</li><li>gamma delta</li></ol>'
    expect(splitPara(ol, (h) => textOf(h).length <= 9)).toEqual(['<ol><li>alpha</li><li>beta</li></ol>', '<ol start="3"><li>gamma delta</li></ol>'])
    // a continued item shows its own number again
    expect(splitPara(ol, (h) => textOf(h).length <= 15)[1]).toBe('<ol start="3"><li>delta</li></ol>')
    // from the list's own start, and items' values
    expect(splitPara('<ol start="5"><li>a</li><li>b</li></ol>', (h) => textOf(h).length <= 1)[1]).toBe('<ol start="6"><li>b</li></ol>')
    const src = '<ol class="sources"><li data-anchor="c1-src-4" value="4">a</li><li data-anchor="c1-src-7" value="7">b</li><li>c</li></ol>'
    expect(splitPara(src, (h) => textOf(h).length <= 1)[1])
      .toBe('<ol class="sources" start="7"><li data-anchor="c1-src-7" value="7">b</li><li>c</li></ol>')
    // a reopened element drops its anchor, whether data-anchor or a raw id
    expect(splitPara('<p data-anchor="p1">aa bb</p>', (h) => textOf(h).length <= 3)).toEqual(['<p data-anchor="p1">aa </p>', '<p>bb</p>'])
    expect(splitPara('<p id="p1">aa bb</p>', (h) => textOf(h).length <= 3)).toEqual(['<p id="p1">aa </p>', '<p>bb</p>'])
    expect(splitPara(src, (h) => textOf(h).length <= 2)[1]).toBe('<ol class="sources" start="8"><li>c</li></ol>')
    // a list block in a book: every continued <ol> starts at its first item's number
    const many = '<ol>' + Array.from({ length: 60 }, (_, k) => `<li>item ${k + 1} ${'z'.repeat(30)}</li>`).join('') + '</ol>'
    const pages = layoutAll([{ kind: 'list', chapter: 1, html: many }], opts)
    expect(pages.length).toBeGreaterThan(2)
    let shown = 0
    for (const it of items(pages)) {
      const m = /^<ol(?: start="(\d+)")?><li>(?:item (\d+))?/.exec(it.html)
      // a fresh item shows its own number; a continued one, the number it had on the page before
      expect(Number(m[1] || 1)).toBe(m[2] ? Number(m[2]) : shown)
      shown = Number([...it.html.matchAll(/item (\d+)/g)].at(-1)[1])
    }
  })
})

describe('spreads and locate', () => {
  const pages = layoutAll(book(2, 4), opts)
  it('puts the covers alone and pairs every inside page', () => {
    const s = spreads(pages, { wide: true })
    expect(s[0]).toEqual({ right: 0 })
    expect(s.at(-1)).toEqual({ left: pages.length - 1 })
    expect(s.flatMap((x) => [x.left, x.right]).filter((k) => k != null)).toEqual(pages.map((_, k) => k))
    for (const x of s.slice(1, -1)) expect(Object.hasOwn(x, 'left') && Object.hasOwn(x, 'right')).toBe(true)
  })
  const fakeBook = (inside) => [{ kind: 'cover' }, ...Array.from({ length: inside }, () => ({})), { kind: 'back' }]
    .map((p, k) => ({ items: [{ blockIndex: k }], ...p }))
  it('an odd inside page count gets a blank right partner; an even one pairs exactly', () => {
    expect(spreads(fakeBook(3), { wide: true }))
      .toEqual([{ right: 0 }, { left: 1, right: 2 }, { left: 3, right: null, blankRight: true }, { left: 4 }])
    expect(spreads(fakeBook(4), { wide: true }))
      .toEqual([{ right: 0 }, { left: 1, right: 2 }, { left: 3, right: 4 }, { left: 5 }])
    expect(spreads(fakeBook(1), { wide: true })).toEqual([{ right: 0 }, { left: 1, right: null, blankRight: true }, { left: 2 }])
    expect(spreads(fakeBook(0), { wide: true })).toEqual([{ right: 0 }, { left: 1 }])
    // with no back cover yet (layout still running), the leftover page waits with a blank partner
    expect(spreads(fakeBook(3).slice(0, 4), { wide: true }))
      .toEqual([{ right: 0 }, { left: 1, right: 2 }, { left: 3, right: null, blankRight: true }])
    for (let n = 0; n < 9; n++) {
      const s = spreads(fakeBook(n), { wide: true })
      for (const x of s.slice(1, -1)) expect(Object.hasOwn(x, 'left') && Object.hasOwn(x, 'right')).toBe(true)
      expect(s.filter((x) => x.blankRight)).toHaveLength(n % 2)
    }
  })
  it('narrow: one page a spread', () => {
    expect(spreads(fakeBook(3), { wide: false })).toEqual([{ right: 0 }, { right: 1 }, { right: 2 }, { right: 3 }, { left: 4 }])
    expect(spreadOf(spreads(fakeBook(3)), 2)).toBe(1)
    expect(spreadOf(spreads(fakeBook(3)), 3)).toBe(2)
    expect(spreadOf(spreads(fakeBook(3)), 9)).toBe(-1)
  })
  it('locate finds the page where a block starts, deferred figures included', () => {
    const blocks = book(2, 4)
    for (let k = 0; k < blocks.length; k++) {
      const at = locate(pages, k)
      expect(at, `block ${k}`).toBeGreaterThanOrEqual(0)
      expect(pages[at].items.some((it) => it.blockIndex === k)).toBe(true)
      expect(pages.slice(0, at).some((p) => p.items.some((it) => it.blockIndex === k))).toBe(false)
    }
    expect(locate(pages, blocks.length)).toBe(-1)
    expect(locate([], 0)).toBe(-1)
  })
})
