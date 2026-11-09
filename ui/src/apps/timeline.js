/* timeline — the Timeline window's maths, pure: how a date reads, where an event sits on the
 * axis (a decimal year; BCE is negative), which events are spans (a range, or a date only known
 * to a decade, a century, a millennium or "c."), an axis fitted to the events with round ticks,
 * zoom and pan, and labels laid out in rows that never overlap. */
import { timelineJob } from '../api.js'
import { pollJob } from './imageJob.js'

const MONTHS = ['January', 'February', 'March', 'April', 'May', 'June', 'July', 'August',
                'September', 'October', 'November', 'December']
// the dates Primer keeps run -10000..2100; the axis may pad a little beyond them, never more
export const LIMIT_MIN = -10500
export const LIMIT_MAX = 2150
const MIN_RANGE = 1
const NICE = [1, 2, 5, 10, 20, 25, 50, 100, 200, 250, 500, 1000, 2000, 2500, 5000, 10000]
const APPROX_YEARS = 5      // "c. 1277" is drawn as 1272..1282

const num = (v) => typeof v === 'number' && Number.isFinite(v)
const validMonth = (m) => Number.isInteger(m) && m >= 1 && m <= 12
const validDay = (d) => Number.isInteger(d) && d >= 1 && d <= 31

function ordinal(n) {
  const t = n % 100
  if (t >= 11 && t <= 13) return `${n}th`
  return `${n}${({ 1: 'st', 2: 'nd', 3: 'rd' })[n % 10] || 'th'}`
}

/* "1282", "300 BCE", "79 CE" (a year before 1000 says which era it is in) */
export function formatYear(y) {
  if (!num(y)) return ''
  y = Math.round(y)
  if (y < 0) return `${-y} BCE`
  if (y === 0) return '1 CE'      // there is no year 0; the axis's 0 is the start of 1 CE
  return y < 1000 ? `${y} CE` : `${y}`
}

function formatRange(y, end) {
  if (y < 0 && end < 0) return `${-y}–${-end} BCE`
  if (y > 0 && end > 0) return y >= 1000 ? `${y}–${end}` : `${y}–${end} CE`
  return `${formatYear(y)} – ${formatYear(end)}`
}

/* how an event's date reads: "4 June 1282", "May 1282", "c. 1277", "1277–1283", "the 1640s" as
 * "1640s", "12th century BCE", "2nd millennium BCE" */
export function formatDate(e) {
  if (!e || !num(e.year)) return ''
  const y = e.year, a = Math.abs(y), bce = y < 0 ? ' BCE' : ''
  if (e.precision === 'century') return `${ordinal(Math.floor((a - 1) / 100) + 1)} century${bce}`
  if (e.precision === 'millennium') return `${ordinal(Math.floor((a - 1) / 1000) + 1)} millennium${bce}`
  if (e.precision === 'decade') return `${Math.floor(a / 10) * 10}s${bce}`
  let out
  if (num(e.end_year) && e.end_year > y) out = formatRange(y, e.end_year)
  else if (e.precision === 'day' && validMonth(e.month) && validDay(e.day)) out = `${e.day} ${MONTHS[e.month - 1]} ${formatYear(y)}`
  else if ((e.precision === 'day' || e.precision === 'month') && validMonth(e.month)) out = `${MONTHS[e.month - 1]} ${formatYear(y)}`
  else out = formatYear(y)
  return e.approx ? `c. ${out}` : out
}

/* a year number on the axis: there is no year 0, so 1 BCE runs 0..1 and 1 CE 1..2 (a BCE year
 * sits at year + 1); labels keep the year numbers */
export const toAxis = (y) => (y < 0 ? y + 1 : y)

/* where an event's point sits: its year, plus the month and day as a fraction of it */
export function datePos(e) {
  let p = toAxis(e.year)
  if (validMonth(e.month)) {
    p += (e.month - 1) / 12
    if (validDay(e.day)) p += (e.day - 1) / 365
  }
  return p
}

/* {start, end, kind} for an event that covers years: 'range' (it has an end year) or 'approx'
 * (only known to a decade, century or millennium, or "c."); null for a plain point */
export function eventSpan(e) {
  const y = e.year
  const on = (start, end, kind) => ({ start: toAxis(start), end: toAxis(end), kind })
  if (num(e.end_year) && e.end_year > y) return on(y, e.end_year, 'range')
  const unit = { decade: 10, century: 100, millennium: 1000 }[e.precision]
  if (unit) { const s = Math.floor(y / unit) * unit; return on(s, s + unit, 'approx') }
  if (e.approx) return on(y - APPROX_YEARS, y + APPROX_YEARS, 'approx')
  return null
}

const dated = (events) => (Array.isArray(events) ? events : []).filter(e => e && num(e.year))

/* round ticks between axis positions min and max: the smallest nice step that leaves room for
 * their labels. Ticks are round year numbers (never 0, which is no year), each at its axis
 * position: {year, pos, label, x}. */
function ticksFor(min, max, width) {
  const room = Math.max(2, Math.floor(width / 90))
  const range = max - min
  const step = NICE.find(s => Math.floor(range / s) + 1 <= room) || NICE[NICE.length - 1]
  const ticks = []
  const first = Math.ceil((min <= 0 ? min - 1 : min) / step) * step
  for (let y = first; toAxis(y) <= max; y += step) {
    const year = y + 0          // no -0
    const pos = toAxis(year)
    if (year === 0 || pos < min) continue
    ticks.push({ year, pos, label: formatYear(year), x: ((pos - min) / range) * width })
  }
  return ticks
}

const build = (min, max, width) => ({ min, max, width, ticks: ticksFor(min, max, width) })

/* x in pixels of `year` on axis `ax`; and back */
export const xOf = (ax, year) => ((year - ax.min) / (ax.max - ax.min)) * ax.width
export const yearAt = (ax, x) => ax.min + (x / ax.width) * (ax.max - ax.min)

/* keep [min, min + range] inside the limits, sliding rather than shrinking */
function within(min, range) {
  range = Math.min(Math.max(range, MIN_RANGE), LIMIT_MAX - LIMIT_MIN)
  min = Math.min(Math.max(min, LIMIT_MIN), LIMIT_MAX - range)
  return [min, min + range]
}

/* {min, max, width, ticks: [{year, label, x}]} fitted to the events (their spans included), with
 * a little room at each end; at least ten years wide; null with no dated events */
export function axis(events, width) {
  const list = dated(events)
  if (!list.length) return null
  let lo = Infinity, hi = -Infinity
  for (const e of list) {
    const s = eventSpan(e), p = datePos(e)
    lo = Math.min(lo, s ? s.start : p)
    hi = Math.max(hi, s ? s.end : p)
  }
  if (hi - lo < 10) { const mid = (lo + hi) / 2; lo = mid - 5; hi = mid + 5 }
  const pad = (hi - lo) * 0.06
  const [min, max] = within(lo - pad, hi - lo + 2 * pad)
  return build(min, max, width)
}

/* zoom by `factor` (2 = twice as close) keeping the year `at` where it is on screen */
export function zoom(ax, factor, at = (ax.min + ax.max) / 2) {
  const old = ax.max - ax.min
  const range = Math.min(Math.max(old / factor, MIN_RANGE), LIMIT_MAX - LIMIT_MIN)
  const [min, max] = within(at - (at - ax.min) * (range / old), range)
  return build(min, max, ax.width)
}

/* move the window `years` later (negative: earlier) */
export function pan(ax, years) {
  const [min, max] = within(ax.min + years, ax.max - ax.min)
  return build(min, max, ax.width)
}

/* a label short enough for a pin: cut at a word, with an ellipsis */
export function shortLabel(s, max = 40) {
  s = String(s || '')
  if (s.length <= max) return s
  let cut = s.slice(0, max - 1)
  const sp = cut.lastIndexOf(' ')
  if (sp > max * 0.5) cut = cut.slice(0, sp)
  return cut.trimEnd() + '…'
}

/* The visible events as rows of pins: [[{index, event, x, lx, text, left, right, flip, span,
 * occL, occR}]]. A label is drawn rightward from lx (the pin, or a range's bar end), or, near the
 * right edge, leftward from lx (flip) when it fits there; [left, right] is the label's box as
 * drawn and [occL, occR] that box plus the span. A span is {x0, x1, kind}. A row never holds
 * two items whose boxes overlap. */
export function layout(events, ax, { charW = 6.6, pad = 16, gap = 8 } = {}) {
  if (!ax) return []
  const W = ax.width
  const items = []
  ;(Array.isArray(events) ? events : []).forEach((event, index) => {
    if (!event || !num(event.year)) return
    const x = xOf(ax, datePos(event))
    const s = eventSpan(event)
    const span = s ? { x0: xOf(ax, s.start), x1: xOf(ax, s.end), kind: s.kind } : null
    if (span ? span.x1 < 0 || span.x0 > W : x < 0 || x > W) return
    const text = shortLabel(event.label)
    const w = Math.min(text.length * charW + pad, W)
    const xc = Math.min(Math.max(x, 0), W)
    // a range's label starts where its bar ends, so the bar never runs under the words
    let lx = span && span.kind === 'range' ? Math.min(Math.max(span.x1, xc), W) : xc
    let left = lx - 4, right = left + w, flip = false
    if (right > W) {
      // near the right edge: leftward from the pin, if it fits there; else rightward as it was
      const r2 = Math.min(xc + 4, W)
      if (r2 - w >= 0) { flip = true; lx = xc; right = r2; left = r2 - w }
    }
    left = Math.max(0, left)
    const occL = Math.min(left, span ? Math.max(0, span.x0) : left)
    const occR = Math.max(right, span ? Math.min(W, span.x1) : right)
    items.push({ index, event, x, lx, text, left, right, flip, span, occL, occR })
  })
  items.sort((a, b) => a.occL - b.occL || a.index - b.index)
  const rows = [], ends = []
  for (const it of items) {
    let r = ends.findIndex(end => end + gap <= it.occL)
    if (r < 0) { r = rows.length; rows.push([]); ends.push(-Infinity) }
    rows[r].push(it)
    ends[r] = it.occR
  }
  return rows
}

export const isEnded = (t) => !!t && (t.status === 'done' || t.status === 'failed')

export function timelineStatusLabel(t) {
  if (!t) return 'Starting…'
  if (t.status === 'reading') return 'Reading the dates…'
  if (t.status === 'failed') return `Failed: ${t.error || 'unknown error'}`
  if (t.status === 'done') {
    const n = (t.events || []).length
    return n ? `${n} ${n === 1 ? 'event' : 'events'}` : 'No dated events on this page'
  }
  return t.status || ''
}

/* All my timelines: each page's colour (an index into the palette), in first-seen order */
export const PALETTE_SIZE = 8
export function pageColours(events, size = PALETTE_SIZE) {
  const out = new Map()
  for (const e of events || []) if (e && !out.has(e.node_id)) out.set(e.node_id, out.size % size)
  return out
}
export function legend(events, size = PALETTE_SIZE) {
  const seen = new Map()
  for (const e of events || []) if (e && !seen.has(e.node_id)) seen.set(e.node_id, e.page_title || 'Untitled')
  const colours = pageColours(events, size)
  return [...seen].map(([node_id, title]) => ({ node_id, title, colour: colours.get(node_id) }))
}

/* poll timeline `id` every second to its end (see imageJob.pollJob) */
export const pollTimeline = (id, onUpdate, opts = {}) => pollJob(id, onUpdate, { fetchJob: timelineJob, ...opts })
