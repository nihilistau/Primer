import { afterEach, describe, expect, test } from 'vitest'
import { layoutAll, spreads } from './layout.js'
import { cleanBlocks } from './sanitize.js'
import {
  WIDE_MIN, aboutTotal, clickZone, linkEvent, anchorPage, announcement, chapterList, chapterSpread, isWide, openBound, pageLabel, pageMetrics,
  pagePicture, placeKey, readPaper, readPlace, readSound, routeLink, sides, spreadChapter, writePlace, writePref,
} from './view.js'
import * as wm from '../windowManager.js'

const MEDIA = '/v1/media/' + 'ab'.repeat(32) + '.png'
const BLOCKS = [
  { kind: 'cover', html: '<section class="cover" id="cover"><h1 class="title">Castles</h1></section>', chapter: 0 },
  { kind: 'contents', html: '<ol class="contents d1"><li><a href="#c1">Bodiam Castle</a></li></ol>', chapter: 0 },
  { kind: 'chapter-head', html: '<div class="chapter-head" id="c1"><h1>Bodiam <em>Castle</em></h1></div>', chapter: 1, node_id: 'n1' },
  { kind: 'para', html: '<p>Moated walls.<sup><a href="#c1-src-1">1</a></sup></p>', chapter: 1 },
  { kind: 'picture', html: `<figure><img src="${MEDIA}" alt="x"></figure>`, chapter: 1 },
  { kind: 'sources', html: '<ol class="sources"><li id="c1-src-1" value="1"><cite>A guide</cite>. <a href="https://example.org/a" data-external="true">https://example.org/a</a></li></ol>', chapter: 1 },
  { kind: 'chapter-head', html: '<div class="chapter-head" id="c2"><h1>Trebuchet</h1></div>', chapter: 2, node_id: 'n2' },
  { kind: 'para', html: '<p>A siege engine.</p>', chapter: 2 },
  { kind: 'back', html: '<section class="back" id="back"><p>Made with Primer</p></section>', chapter: 0 },
]
// one block a page: every block is as tall as a page. The blocks are cleaned first, as the viewer
// does, so their ids are data-anchor attributes
const PAGES = layoutAll(cleanBlocks(BLOCKS), { measure: () => 100, pageHeight: 100 })

describe('spread selection by width', () => {
  test('two-page spreads at 900 px and over, otherwise one page', () => {
    expect(WIDE_MIN).toBe(900)
    expect(isWide(899)).toBe(false)
    expect(isWide(900)).toBe(true)
    expect(isWide(1400)).toBe(true)
    expect(isWide(undefined)).toBe(false)
  })

  test('a wide spread shows both its pages; a narrow one shows one', () => {
    expect(sides({ left: 3, right: 4 }, true)).toEqual({ left: 3, right: 4, blankRight: false })
    expect(sides({ left: 5, right: null, blankRight: true }, true)).toEqual({ left: 5, right: null, blankRight: true })
    expect(sides({ right: 0 }, true)).toEqual({ left: null, right: 0, blankRight: false })
    expect(sides({ right: 4 }, false)).toEqual({ left: null, right: 4 })
    expect(sides({ left: 9 }, false)).toEqual({ left: null, right: 9 }) // the back cover, alone
  })

  test('pages keep the A5 proportion and fit the stage', () => {
    for (const [w, h, wide] of [[1000, 700, true], [600, 900, false], [1600, 500, true], [320, 480, false]]) {
      const m = pageMetrics(w, h, wide)
      expect(m.ph / m.pw).toBeCloseTo(1.414, 2)
      expect(m.ph).toBeLessThanOrEqual(Math.max(h, 230))
      expect(m.pw * (wide ? 2 : 1)).toBeLessThanOrEqual(Math.max(w, 160))
      expect(m.contentH).toBe(m.ph - m.padT - m.padB)
      expect(Number.isInteger(m.contentH) && Number.isInteger(m.contentW)).toBe(true)
    }
  })
})

describe('labels', () => {
  test('the page label and the announcement', () => {
    expect(pageLabel({ left: 9, right: 10 }, 48)).toBe('Pages 10–11 of 48')
    expect(pageLabel({ right: 0 }, 48)).toBe('Page 1 of 48')
    expect(pageLabel({ left: 8, right: null, blankRight: true }, 48)).toBe('Page 9 of 48')
    expect(pageLabel({ left: 1, right: 2 }, 40, false)).toBe('Pages 2–3 of about 40')
    expect(announcement('Pages 10–11 of 48', 'Bodiam Castle')).toBe('Pages 10–11 of 48, Chapter: Bodiam Castle')
    expect(announcement('Page 1 of 48', '')).toBe('Page 1 of 48')
  })

  test('the page count is estimated while the layout runs', () => {
    expect(aboutTotal(10, 25, 100)).toBe(40)
    expect(aboutTotal(10, 100, 100)).toBe(10)
    expect(aboutTotal(0, 0, 100)).toBe(0)
  })

  test('chapters come from the chapter heads, with their text', () => {
    expect(chapterList(BLOCKS)).toEqual([
      { chapter: 1, title: 'Bodiam Castle', blockIndex: 2, nodeId: 'n1' },
      { chapter: 2, title: 'Trebuchet', blockIndex: 6, nodeId: 'n2' },
    ])
  })

  test('PgUp and PgDn go to chapter starts', () => {
    const list = spreads(PAGES, { wide: false })
    const c1 = PAGES.findIndex((p) => p.chapter === 1)
    const c2 = PAGES.findIndex((p) => p.chapter === 2)
    expect(chapterSpread(list, PAGES, 0, 1)).toBe(1)          // the cover to the contents
    expect(chapterSpread(list, PAGES, c1, 1)).toBe(c2)
    expect(chapterSpread(list, PAGES, c1 + 2, -1)).toBe(c1)   // inside chapter 1: back to its start
    expect(chapterSpread(list, PAGES, c1, -1)).toBe(1)        // from its start: the part before
    expect(chapterSpread(list, PAGES, list.length - 1, 1)).toBe(-1)
    expect(spreadChapter(list[c2], PAGES)).toBe(2)
  })
})

describe('link routing', () => {
  test('a contents entry turns to its chapter, a citation to its sources page', () => {
    const c1 = PAGES.findIndex((p) => p.items.some((it) => it.blockIndex === 2))
    const src = PAGES.findIndex((p) => p.items.some((it) => it.blockIndex === 5))
    expect(routeLink('#c1', PAGES)).toEqual({ go: 'page', page: c1 })
    expect(routeLink('#c1-src-1', PAGES)).toEqual({ go: 'page', page: src })
    expect(routeLink('#back', PAGES)).toEqual({ go: 'page', page: PAGES.length - 1 })
    // no page carries a real id; the anchors are data-anchor
    for (const p of PAGES) for (const it of p.items) expect(it.html).not.toMatch(/\sid=/)
    expect(PAGES[c1].items[0].html).toContain('data-anchor="c1"')
  })

  test('an anchor not laid out yet waits while the layout runs, and is nothing once done', () => {
    expect(routeLink('#c7', PAGES, false)).toEqual({ go: 'wait', id: 'c7' })
    expect(routeLink('#c7', PAGES, true)).toEqual({ go: 'none' })
    expect(anchorPage(PAGES, 'c2')).toBe(PAGES.findIndex((p) => p.chapter === 2))
  })

  test('an http(s) link only offers the Browser window', () => {
    expect(routeLink('https://example.org/a', PAGES)).toEqual({ go: 'browser', url: 'https://example.org/a' })
    expect(routeLink('http://example.org/', PAGES)).toEqual({ go: 'browser', url: 'http://example.org/' })
  })

  test('anything else goes nowhere', () => {
    for (const h of ['javascript:alert(1)', 'data:text/html,x', '//example.org', '#', '#1x', '#a"b', 'mailto:a@b', '', null, 'https://a b']) {
      expect(routeLink(h, PAGES)).toEqual({ go: 'none' })
    }
  })

  test('a thumbnail takes the first of Primer\'s own pictures', () => {
    expect(pagePicture({ items: [{ html: '<p>x</p>' }, { html: `<figure><img alt="" src="${MEDIA}"></figure>` }] })).toBe(MEDIA)
    expect(pagePicture({ items: [{ html: '<img src="https://example.org/x.png">' }] })).toBe('')
    expect(pagePicture(null)).toBe('')
  })
})

describe('remembered settings and place', () => {
  const memory = () => {
    const m = new Map()
    return { getItem: (k) => (m.has(k) ? m.get(k) : null), setItem: (k, v) => m.set(k, String(v)), m }
  }
  const broken = { getItem: () => { throw new Error('denied') }, setItem: () => { throw new Error('full') } }

  test('night paper and sound off by default, remembered when changed', () => {
    const s = memory()
    expect(readPaper(s)).toBe('night')
    expect(readSound(s)).toBe(false)
    writePref(s, 'primer.bound.paper', 'day')
    writePref(s, 'primer.bound.sound', 'on')
    expect(readPaper(s)).toBe('day')
    expect(readSound(s)).toBe(true)
  })

  test('storage that throws, or none, falls back quietly', () => {
    expect(readPaper(broken)).toBe('night')
    expect(readSound(null)).toBe(false)
    expect(() => writePref(broken, 'k', 'v')).not.toThrow()
    expect(() => writePlace(broken, 'k', 3)).not.toThrow()
    expect(readPlace(broken, 'k')).toBe(null)
  })

  test('the place is kept per book id, or per scope for the live trail', () => {
    expect(placeKey({ bookId: 'abc' })).toBe('primer.bound.place.book:abc')
    expect(placeKey({ scope: { node_id: 'n1', kind: 'branch' } })).toBe(placeKey({ scope: { kind: 'branch', node_id: 'n1' } }))
    expect(placeKey({ scope: { kind: 'all' } })).not.toBe(placeKey({ scope: { kind: 'branch', node_id: 'n1' } }))
    expect(placeKey({})).toBe('')
    // the live trail's place follows its include switches too (order-free); a made book's does not
    const sc = { kind: 'branch', node_id: 'n1' }
    expect(placeKey({ scope: sc, include: { pictures: true, maps: false } }))
      .toBe(placeKey({ scope: sc, include: { maps: false, pictures: true } }))
    expect(placeKey({ scope: sc, include: { pictures: true, maps: false } }))
      .not.toBe(placeKey({ scope: sc, include: { pictures: false, maps: false } }))
    expect(placeKey({ scope: sc, include: { pictures: true } })).not.toBe(placeKey({ scope: sc }))
    expect(placeKey({ bookId: 'abc', include: { pictures: true } })).toBe('primer.bound.place.book:abc')
    const s = memory()
    writePlace(s, 'k', 12)
    expect(readPlace(s, 'k')).toEqual({ block: 12 })
    s.setItem('k', '{bad json')
    expect(readPlace(s, 'k')).toBe(null)
    s.setItem('k', '{"block":-1}')
    expect(readPlace(s, 'k')).toBe(null)
  })
})

describe('opening a bound window', () => {
  afterEach(() => wm._reset())
  test('each open is a new bound window with its props', () => {
    const a = openBound({ bookId: 'b1' })
    const b = openBound({ scope: { kind: 'all' }, include: {} })
    expect(a).not.toBe(b)
    const ws = wm.getWindows()
    expect(ws.map((w) => w.appId)).toEqual(['bound', 'bound'])
    expect(ws[0].props).toEqual({ bookId: 'b1' })
  })
})

describe('link events and click zones', () => {
  const ev = (type, onLink, button = 0) => {
    const a = { getAttribute: () => '#c3' }
    const e = { type, button, prevented: false, target: { closest: (sel) => (onLink && sel === 'a' ? a : null) } }
    e.preventDefault = () => { e.prevented = true }
    return e
  }
  test('a middle-click on a link never opens a new tab: it is routed like a click', () => {
    const went = []
    const e = ev('auxclick', true, 1)
    expect(linkEvent(e, (h) => went.push(h))).toBe(true)
    expect(e.prevented).toBe(true)
    expect(went).toEqual(['#c3'])
    const c = ev('click', true)
    linkEvent(c, (h) => went.push(h))
    expect(c.prevented).toBe(true)
    expect(went).toEqual(['#c3', '#c3'])
  })
  test('other buttons and drags on a link only lose their default', () => {
    const went = []
    for (const e of [ev('auxclick', true, 2), ev('dragstart', true)]) {
      expect(linkEvent(e, (h) => went.push(h))).toBe(true)
      expect(e.prevented).toBe(true)
    }
    expect(went).toEqual([])
  })
  test('off a link nothing is prevented', () => {
    const e = ev('auxclick', false, 1)
    expect(linkEvent(e, () => {})).toBe(false)
    expect(e.prevented).toBe(false)
  })
  test('clicks turn from the outer third of the outer pages', () => {
    const wide = { left: 100, right: 900, width: 800 } // pages of 400
    expect(clickZone(850, wide, true)).toBe(1)
    expect(clickZone(150, wide, true)).toBe(-1)
    expect(clickZone(500, wide, true)).toBe(0)
    const narrow = { left: 100, right: 500, width: 400 }
    expect(clickZone(480, narrow, false)).toBe(1)
    expect(clickZone(120, narrow, false)).toBe(-1)
    expect(clickZone(300, narrow, false)).toBe(0)
    expect(clickZone(300, null, false)).toBe(0)
  })
})
