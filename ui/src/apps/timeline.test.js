import { describe, expect, it } from 'vitest'
import { axis, datePos, eventSpan, formatDate, formatYear, layout, legend, pageColours, pan, shortLabel,
         timelineStatusLabel, xOf, zoom } from './timeline.js'

const ev = (year, more = {}) => ({ label: `event ${year}`, year, month: null, day: null, end_year: null,
                                   precision: 'year', approx: false, ...more })

describe('formatYear', () => {
  it('names BCE years and early CE years', () => {
    expect(formatYear(1282)).toBe('1282')
    expect(formatYear(-300)).toBe('300 BCE')
    expect(formatYear(79)).toBe('79 CE')
    expect(formatYear(0)).toBe('1 CE')
  })
})

describe('formatDate', () => {
  it('reads each precision', () => {
    expect(formatDate(ev(1282, { month: 6, day: 4, precision: 'day' }))).toBe('4 June 1282')
    expect(formatDate(ev(1282, { month: 5, precision: 'month' }))).toBe('May 1282')
    expect(formatDate(ev(1282))).toBe('1282')
    expect(formatDate(ev(1640, { precision: 'decade' }))).toBe('1640s')
    expect(formatDate(ev(1992, { precision: 'decade' }))).toBe('1990s')
    expect(formatDate(ev(1150, { precision: 'century' }))).toBe('12th century')
    expect(formatDate(ev(1110, { precision: 'century' }))).toBe('12th century')
    expect(formatDate(ev(-1150, { precision: 'century' }))).toBe('12th century BCE')
    expect(formatDate(ev(2050, { precision: 'century' }))).toBe('21st century')
    expect(formatDate(ev(250, { precision: 'century' }))).toBe('3rd century')
    expect(formatDate(ev(150, { precision: 'century' }))).toBe('2nd century')
    expect(formatDate(ev(1050, { precision: 'century' }))).toBe('11th century')
    expect(formatDate(ev(1250, { precision: 'century' }))).toBe('13th century')
    expect(formatDate(ev(500, { precision: 'millennium' }))).toBe('1st millennium')
    expect(formatDate(ev(-1500, { precision: 'millennium' }))).toBe('2nd millennium BCE')
  })
  it('marks approximate dates and ranges', () => {
    expect(formatDate(ev(1277, { approx: true }))).toBe('c. 1277')
    expect(formatDate(ev(-300))).toBe('300 BCE')
    expect(formatDate(ev(1277, { end_year: 1283 }))).toBe('1277–1283')
    expect(formatDate(ev(-300, { end_year: -200 }))).toBe('300–200 BCE')
    expect(formatDate(ev(-30, { end_year: 14 }))).toBe('30 BCE – 14 CE')
    expect(formatDate(ev(1277, { end_year: 1283, approx: true }))).toBe('c. 1277–1283')
  })
  it('never throws on a malformed event', () => {
    expect(formatDate(null)).toBe('')
    expect(formatDate({})).toBe('')
    expect(formatDate(ev(1282, { month: 13, precision: 'month' }))).toBe('1282')
  })
})

describe('datePos and eventSpan', () => {
  it('places months and days within the year', () => {
    expect(datePos(ev(1282))).toBe(1282)
    expect(datePos(ev(1282, { month: 7, precision: 'month' }))).toBeCloseTo(1282.5, 1)
    // there is no year 0: 1 BCE runs 0..1 on the axis and 1 CE 1..2, so a BCE year sits at year + 1
    expect(datePos(ev(-300))).toBe(-299)
    expect(datePos(ev(-1))).toBe(0)
    expect(datePos(ev(1))).toBe(1)
  })
  it('a range, a coarse precision or an approximate year is a span; a plain year is not', () => {
    expect(eventSpan(ev(1277, { end_year: 1283 }))).toEqual({ start: 1277, end: 1283, kind: 'range' })
    expect(eventSpan(ev(1150, { precision: 'century' }))).toEqual({ start: 1100, end: 1200, kind: 'approx' })
    expect(eventSpan(ev(-1150, { precision: 'century' }))).toEqual({ start: -1199, end: -1099, kind: 'approx' })
    expect(eventSpan(ev(1640, { precision: 'decade' }))).toEqual({ start: 1640, end: 1650, kind: 'approx' })
    expect(eventSpan(ev(-1500, { precision: 'millennium' }))).toEqual({ start: -1999, end: -999, kind: 'approx' })
    // 30 BCE to 14 CE is 43 years, not 44
    const r = eventSpan(ev(-30, { end_year: 14 }))
    expect(r.end - r.start).toBe(43)
    expect(eventSpan(ev(1277, { approx: true }))).toEqual({ start: 1272, end: 1282, kind: 'approx' })
    expect(eventSpan(ev(1277))).toBeNull()
    expect(eventSpan(ev(1282, { month: 6, day: 4, precision: 'day' }))).toBeNull()
  })
})

describe('axis', () => {
  const sane = (ax, width) => {
    expect(ax.width).toBe(width)
    expect(ax.min).toBeLessThan(ax.max)
    expect(ax.ticks.length).toBeGreaterThanOrEqual(2)
    expect(ax.ticks.length).toBeLessThanOrEqual(Math.floor(width / 60) + 1)
    ax.ticks.forEach((t, i) => {
      expect(t.year).not.toBe(0)
      expect(t.pos).toBeGreaterThanOrEqual(ax.min)
      expect(t.pos).toBeLessThanOrEqual(ax.max)
      expect(t.pos).toBe(t.year < 0 ? t.year + 1 : t.year)
      expect(t.x).toBeCloseTo(xOf(ax, t.pos))
      expect(typeof t.label).toBe('string')
      if (i) expect(t.year).toBeGreaterThan(ax.ticks[i - 1].year)
    })
  }
  it('is null with no events', () => {
    expect(axis([], 800)).toBeNull()
    expect(axis(null, 800)).toBeNull()
  })
  it('fits a short span with every event inside', () => {
    const ax = axis([ev(1277), ev(1283)], 800)
    sane(ax, 800)
    expect(ax.min).toBeLessThan(1277)
    expect(ax.max).toBeGreaterThan(1283)
    expect(xOf(ax, 1277)).toBeGreaterThan(0)
    expect(xOf(ax, 1283)).toBeLessThan(800)
  })
  it('one event still gets a span of years around it', () => {
    const ax = axis([ev(1066)], 600)
    sane(ax, 600)
    expect(ax.max - ax.min).toBeGreaterThanOrEqual(10)
    expect(ax.min).toBeLessThan(1066)
    expect(ax.max).toBeGreaterThan(1066)
  })
  it('crosses into BCE with nice round ticks and BCE labels', () => {
    const ax = axis([ev(-300), ev(100)], 800)
    sane(ax, 800)
    expect(ax.ticks.some(t => /BCE/.test(t.label))).toBe(true)
    const step = ax.ticks[1].year - ax.ticks[0].year
    const nice = [1, 2, 5, 10, 20, 25, 50, 100, 200, 250, 500, 1000, 2000, 2500, 5000]
    expect(nice).toContain(step)
    ax.ticks.forEach(t => expect(Math.abs(t.year % step)).toBe(0))
  })
  it('1-year ticks across the era boundary never repeat a label or tick year 0', () => {
    const ax = axis([ev(-3), ev(3)], 1400)
    sane(ax, 1400)
    expect(ax.ticks[1].year - ax.ticks[0].year === 1 || ax.ticks[2].year - ax.ticks[1].year === 1).toBe(true)
    const labels = ax.ticks.map(t => t.label)
    expect(new Set(labels).size).toBe(labels.length)
    expect(labels).toContain('1 BCE')
    expect(labels).toContain('1 CE')
    expect(labels.indexOf('1 BCE') + 1).toBe(labels.indexOf('1 CE'))
  })
  it('a long span ticks in centuries or millennia', () => {
    const ax = axis([ev(-3000), ev(2000)], 900)
    sane(ax, 900)
    expect(ax.ticks[1].year - ax.ticks[0].year).toBeGreaterThanOrEqual(500)
  })
  it('makes room for a span, not just its point', () => {
    const ax = axis([ev(1150, { precision: 'century' }), ev(1300)], 800)
    expect(ax.min).toBeLessThanOrEqual(1100)
    const r = axis([ev(1277, { end_year: 1400 })], 800)
    expect(r.max).toBeGreaterThanOrEqual(1400)
  })
})

describe('zoom and pan', () => {
  const ax = axis([ev(1200), ev(1400)], 800)
  it('zooming in keeps the year under the cursor where it was', () => {
    const at = 1300, before = xOf(ax, at)
    const z = zoom(ax, 2, at)
    expect(z.max - z.min).toBeCloseTo((ax.max - ax.min) / 2)
    expect(xOf(z, at)).toBeCloseTo(before)
    expect(z.width).toBe(800)
    expect(z.ticks.length).toBeGreaterThanOrEqual(2)
  })
  it('zooming out then in returns to the same span', () => {
    const z = zoom(zoom(ax, 0.5, 1250), 2, 1250)
    expect(z.min).toBeCloseTo(ax.min)
    expect(z.max).toBeCloseTo(ax.max)
  })
  it('cannot zoom in past a year or out past the whole range of dates', () => {
    let z = ax
    for (let i = 0; i < 40; i++) z = zoom(z, 2, 1300)
    expect(z.max - z.min).toBeGreaterThanOrEqual(1)
    for (let i = 0; i < 60; i++) z = zoom(z, 0.5, 1300)
    expect(z.max - z.min).toBeLessThanOrEqual(13000)
    expect(z.min).toBeGreaterThanOrEqual(-11000)
    expect(z.max).toBeLessThanOrEqual(2200)
  })
  it('pan shifts the window by a number of years', () => {
    const p = pan(ax, 50)
    expect(p.min).toBeCloseTo(ax.min + 50)
    expect(p.max).toBeCloseTo(ax.max + 50)
  })
})

describe('layout', () => {
  const overlap = (a, b) => a.left < b.right && b.left < a.right
  it('stacks events that would collide and keeps far ones on one row', () => {
    const events = [ev(1300), ev(1300), ev(1200), ev(1400)]
    const ax = axis(events, 1000)
    const rows = layout(events, ax)
    const placed = rows.flat()
    expect(placed.map(p => p.index).sort()).toEqual([0, 1, 2, 3])
    const r0 = rows.findIndex(r => r.some(p => p.index === 0)), r1 = rows.findIndex(r => r.some(p => p.index === 1))
    expect(r0).not.toBe(r1)
    expect(rows.length).toBeLessThanOrEqual(2)
  })
  it('never overlaps two labels on a row, however crowded', () => {
    const events = Array.from({ length: 40 }, (_, i) => ev(1000 + ((i * 37) % 300), { label: 'x'.repeat(5 + (i % 30)) }))
    const ax = axis(events, 700)
    const rows = layout(events, ax)
    expect(rows.flat().length).toBe(40)
    rows.forEach(r => r.forEach((a, i) => r.slice(i + 1).forEach(b => expect(overlap(a, b)).toBe(false))))
    const occ = (a, b) => a.occL < b.occR && b.occL < a.occR
    rows.forEach(r => r.forEach((a, i) => r.slice(i + 1).forEach(b => expect(occ(a, b)).toBe(false))))
    rows.flat().forEach(p => { expect(p.occL).toBeLessThanOrEqual(p.left); expect(p.occR).toBeGreaterThanOrEqual(p.right) })
    rows.flat().forEach(p => {
      expect(p.left).toBeGreaterThanOrEqual(0)
      expect(p.right).toBeLessThanOrEqual(700)
    })
  })
  it('a label near the right edge opens to the left of its pin', () => {
    const events = [ev(1000), ev(2000, { label: 'a long label at the very end of time' })]
    const ax = axis(events, 400)
    const p = layout(events, ax).flat().find(q => q.index === 1)
    expect(p.flip).toBe(true)
    expect(p.right).toBeLessThanOrEqual(400)
  })
  it('a label that fits on neither side of its pin opens rightward from it, and its box is what is drawn', () => {
    const long = 'a label of forty characters, give or take'
    const events = [ev(1000), ev(1150, { label: long }), ev(2000)]
    const ax = axis(events, 300)
    const p = layout(events, ax).flat().find(q => q.index === 1)
    expect(p.flip).toBe(false)
    expect(p.lx).toBeCloseTo(p.x)
    expect(p.left).toBeCloseTo(p.x - 4)
    expect(p.right - p.left).toBeCloseTo(shortLabel(long).length * 6.6 + 16)
    expect(p.occR).toBeGreaterThanOrEqual(p.right)
  })
  it('gives a span its pixel extent', () => {
    const events = [ev(1277, { end_year: 1283 }), ev(1300)]
    const ax = axis(events, 800)
    const p = layout(events, ax).flat().find(q => q.index === 0)
    expect(p.span.x0).toBeCloseTo(xOf(ax, 1277))
    expect(p.span.x1).toBeCloseTo(xOf(ax, 1283))
    expect(p.span.kind).toBe('range')
  })
  it("a range's label starts after its bar, not over it", () => {
    const events = [ev(1100), ev(1150, { end_year: 1170, label: 'works' }), ev(1300)]
    const ax = axis(events, 800)
    const p = layout(events, ax).flat().find(q => q.index === 1)
    expect(p.flip).toBe(false)
    expect(p.lx).toBeCloseTo(xOf(ax, 1170))
    expect(p.left).toBeGreaterThanOrEqual(xOf(ax, 1170) - 4)
    const q = layout(events, ax).flat().find(r => r.index === 0)
    expect(q.lx).toBeCloseTo(q.x)
  })
  it('skips events outside the visible window', () => {
    const events = [ev(1000), ev(1500)]
    const ax = zoom(axis(events, 800), 8, 1000)
    const ids = layout(events, ax).flat().map(p => p.index)
    expect(ids).toEqual([0])
  })
  it('shortLabel cuts long labels at a word', () => {
    expect(shortLabel('short')).toBe('short')
    const s = shortLabel('the siege of Caernarfon castle and the building of the town walls', 30)
    expect(s.length).toBeLessThanOrEqual(30)
    expect(s.endsWith('…')).toBe(true)
  })
})

describe('status and colours', () => {
  it('reads a timeline job', () => {
    expect(timelineStatusLabel(null)).toBe('Starting…')
    expect(timelineStatusLabel({ status: 'reading' })).toBe('Reading the dates…')
    expect(timelineStatusLabel({ status: 'done', events: [] })).toBe('No dated events on this page')
    expect(timelineStatusLabel({ status: 'done', events: [ev(1)] })).toBe('1 event')
    expect(timelineStatusLabel({ status: 'done', events: [ev(1), ev(2)] })).toBe('2 events')
    expect(timelineStatusLabel({ status: 'failed', error: 'No fast model in this tier' })).toBe('Failed: No fast model in this tier')
  })
  it('colours events by page, in first-seen order, cycling the palette', () => {
    const evs = [{ node_id: 'a', page_title: 'A' }, { node_id: 'b', page_title: 'B' }, { node_id: 'a', page_title: 'A' }]
    const c = pageColours(evs)
    expect(c.get('a')).toBe(0)
    expect(c.get('b')).toBe(1)
    const many = Array.from({ length: 10 }, (_, i) => ({ node_id: `n${i}`, page_title: `P${i}` }))
    expect(pageColours(many).get('n8')).toBe(0)
    expect(legend(evs)).toEqual([{ node_id: 'a', title: 'A', colour: 0 }, { node_id: 'b', title: 'B', colour: 1 }])
  })
})
