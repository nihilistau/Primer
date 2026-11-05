/* readerModel — the pure parts of the Reader, Review and Quest windows and of Test me: the
 * counted bars, due labels, marks, the answer counter, error words, the Quest's rooms, and
 * the dwell tracker the page reader reports visible time with. No React, no fetch. */

const DAY_S = 86400
const HOUR_S = 3600

export const ANSWER_CHARS = 2000
export const NOTES_CHARS = 1000
export const VISIT_CAP_S = 600       // the server caps a visit at 600 s; never send more
export const DWELL_EVERY_S = 30

/* a window event: an answer was marked somewhere, so the dock's due count may have moved */
export const QUIZ_EVENT = 'primer:quiz'
export const announceQuiz = () => { if (typeof window !== 'undefined') window.dispatchEvent(new Event(QUIZ_EVENT)) }

/* a window event: the Reader window reset the reader, so quiz items, due counts and quests
 * on screen are gone; the dock badge, Review, Test me and Quest read again */
export const READER_RESET_EVENT = 'primer:reader-reset'
export const announceReaderReset = () => { if (typeof window !== 'undefined') window.dispatchEvent(new Event(READER_RESET_EVENT)) }
/* listen for it; returns the unsubscribe */
export function onReaderReset(fn) {
  if (typeof window === 'undefined') return () => {}
  window.addEventListener(READER_RESET_EVENT, fn)
  return () => window.removeEventListener(READER_RESET_EVENT, fn)
}

/* Check after Show answer: the server marks it not yet without asking the model */
export const REVEALED_NOTE = 'You looked at the answer: this one comes back tomorrow.'

/* the Reader window's note on which hosted model gets what ("" when both are local) */
export function hostedNote(st) {
  const models = Array.isArray(st && st.hosted_models) ? st.hosted_models : (st && st.hosted ? ['writing'] : [])
  const out = []
  if (models.includes('writing')) out.push('This profile goes to the hosted writing model with every page request.')
  if (models.includes('fast')) out.push('Page titles, counts and quiz results go to the hosted fast model to write the summary.')
  return out.join(' ')
}

const frac = (x) => (Number.isFinite(x) ? Math.min(1, Math.max(0, x)) : 0)
const count = (x) => (Number.isFinite(x) && x > 0 ? x : 0)

/* the three branch styles as bars, in a fixed order; [] before any branch was opened */
const STYLES = [['context', 'In this context'], ['general', 'General'], ['sideways', 'Sideways']]
export function shareBars(style) {
  if (!style || !(style.branches > 0)) return []
  return STYLES.map(([key, label]) => {
    const share = frac(style[key])
    return { key, label, share, pct: Math.round(share * 100), lean: style.lean === key }
  })
}

/* Deeper vs Simpler asks, each against their total, and the lean in words */
export function levelBars(level) {
  const l = level || {}
  const deeper = count(l.deeper), simpler = count(l.simpler), total = deeper + simpler
  const bar = (key, label, n) => ({ key, label, n, share: total ? n / total : 0, lean: l.lean === key })
  const text = !total ? 'No Deeper or Simpler asks yet'
    : l.lean === 'deeper' ? 'Asks for deeper pages more often'
    : l.lean === 'simpler' ? 'Asks for simpler pages more often' : 'No clear lean'
  return { bars: [bar('deeper', 'Deeper', deeper), bar('simpler', 'Simpler', simpler)], text }
}

/* "59 s", "2 min", "1 h 2 min" ("" for nothing) */
export function formatDuration(seconds) {
  const s = Math.round(count(seconds))
  if (!s) return ''
  if (s < 60) return `${s} s`
  const m = Math.floor(s / 60)
  if (m < 60) return `${m} min`
  const h = Math.floor(m / 60), r = m % 60
  return r ? `${h} h ${r} min` : `${h} h`
}

/* the most-read topics, opens scaled to the top one, with the time spent */
export function topicBars(topics) {
  const list = Array.isArray(topics) ? topics : []
  const top = Math.max(0, ...list.map(t => count(t.opens)))
  return list.map(t => ({ title: t.title, opens: count(t.opens), share: top ? count(t.opens) / top : 0, time: formatDuration(t.seconds) }))
}

/* "in 3 days" / "in 5 hours" / "in under an hour" / "now" for a due time (s) against now (ms) */
function inWords(dueAt, now) {
  const left = dueAt - now / 1000
  if (left <= 0) return 'now'
  if (left < HOUR_S) return 'in under an hour'
  if (left < DAY_S) { const h = Math.round(left / HOUR_S); return `in ${h} hour${h === 1 ? '' : 's'}` }
  const d = Math.round(left / DAY_S)
  return `in ${d} day${d === 1 ? '' : 's'}`
}

/* when a question comes back: "next due in 3 days", "due now" */
export function dueLabel(item, now) {
  if (!item || !Number.isFinite(item.due_at)) return ''
  const w = inWords(item.due_at, now)
  return w === 'now' ? 'due now' : `next due ${w}`
}

/* Review with nothing due: when the next one comes back, from the counts' next_due_at or
 * the soonest due time of answers marked here since (whichever is sooner). */
export function emptyReviewText(counts, soonest, now) {
  if (counts && counts.total === 0) return 'Nothing due — use Test me on a page to make some questions'
  const times = [soonest, counts && counts.next_due_at].filter(Number.isFinite)
  const next = times.length ? Math.min(...times) : null
  if (next !== null && inWords(next, now) !== 'now') return `Nothing due — next ${inWords(next, now)}`
  return 'Nothing due'
}

const MARKS = { right: { text: 'Right', tone: 'ok' }, partly: { text: 'Partly', tone: 'warm' }, not_yet: { text: 'Not yet', tone: 'warn' } }
export function markLabel(mark) {
  return MARKS[mark] ? { ...MARKS[mark] } : { text: mark ? String(mark) : '', tone: 'neutral' }
}

/* an answer box's counter and whether Check may send it (1-2000 characters, not only spaces) */
export function answerCheck(text) {
  const t = typeof text === 'string' ? text : ''
  const c = `${t.length}/${ANSWER_CHARS}`
  if (!t.trim()) return { ok: false, count: c, reason: 'Type an answer first' }
  if (t.length > ANSWER_CHARS) return { ok: false, count: c, reason: `At most ${ANSWER_CHARS} characters` }
  return { ok: true, count: c, reason: '' }
}

/* a quiz or quest request's failure in the reader's words; a 503 carries the engine's reason */
export function quizErrorText(e) {
  const status = e && e.status
  if (status === 409) return 'Reset happened; try again'
  if (status === 502) return "The model's reply couldn't be read; try again"
  return String((e && e.message) || e)
}

/* a Quest request's failure: its 409s mean different things (being built, moved on, too
 * early to skip), so the server's own words stay; only an unreadable reply is renamed */
export function questErrorText(e) {
  if (e && e.status === 502) return quizErrorText(e)
  return String((e && e.message) || e)
}

/* the reader's Test me button: a finished page and a writing model that isn't down. With no
 * status yet (or a failed check) it stays enabled: the request says why if it can't. */
const TEXT_DOWN = ['failed', 'unavailable']
export function testMeButton(status, { nodeId, streaming } = {}) {
  if (!nodeId) return { enabled: false, reason: 'Open a page first' }
  if (streaming) return { enabled: false, reason: 'Wait for the page to finish' }
  if (!status || !Array.isArray(status.servers)) return { enabled: true, reason: '' }
  const text = status.servers.find(s => s.role === 'text')
  if (!text) return { enabled: false, reason: 'The writing model is unavailable' }
  if (TEXT_DOWN.includes(text.state)) {
    return { enabled: false, reason: 'The writing model is unavailable' + (text.reason ? `: ${text.reason}` : '') }
  }
  return { enabled: true, reason: '' }
}

/* the Reader window's summary line, or why there is none yet */
export function summaryLine(st) {
  if (st.summary) return { text: st.summary, dim: false }
  if (st.writing) return { text: 'Writing…', dim: true }
  if (st.error) return { text: `Not written yet: ${st.error}`, dim: true }
  if (st.fast && !st.fast.ok) return { text: `Not written yet: ${st.fast.reason}`, dim: true }
  if (!st.counted || (st.counted.opens || 0) < 3) return { text: 'Not enough reading yet', dim: true }
  return { text: 'Not written yet: it is written every few pages, or press Refresh', dim: true }
}

/* the dock's count on Review */
export const dueBadge = (n) => (!(n > 0) ? '' : n > 99 ? '99+' : String(n))

/* a Quest room as the window draws it: locked (no text), passed (with the answer), or the
 * current one (its hint after a miss, the reread nudge after two, Skip after three) */
export function questRoomView(room) {
  if (!room || room.locked) return { kind: 'locked', index: room ? room.index : 0 }
  if (room.opened || room.skipped) {
    return { kind: 'passed', index: room.index, status: room.opened ? 'opened' : 'skipped', title: room.title,
             description: room.description, question: room.question, answer: room.answer || '' }
  }
  const tries = room.tries || 0
  return { kind: 'current', index: room.index, nodeId: room.node_id, title: room.title, description: room.description,
           question: room.question, tries, hint: room.hint || '', reread: tries >= 2, canSkip: tries >= 3 }
}

export function questProgress(q) {
  if (!q || q.status !== 'building') return ''
  const b = q.building
  return b && b.total ? `Building the rooms… ${b.done} of ${b.total}` : 'Building the rooms…'
}

/* DWELL. Counts the seconds a page is visible and reports them for its node: when 30 s have
 * piled up (call tick() often), on a node change, when hidden, on flush() (the tab is going
 * away, but may come back from the back/forward cache: the visit goes on) and on leave().
 * A visit (one node, until another is shown) never reports more than 600 s.
 * report(nodeId, seconds) may throw or reject; failures are dropped. now() is in ms. */
export function dwellTracker({ report, now }) {
  let node = null, shown = true, since = null, pending = 0, sent = 0

  const accrue = () => {
    if (node && shown && since !== null) {
      const t = now()
      pending += Math.max(0, (t - since) / 1000)
      since = t
    }
    pending = Math.min(pending, VISIT_CAP_S - sent)
  }
  const flush = () => {
    accrue()
    if (node && pending >= 1) {
      const s = Math.round(pending * 10) / 10
      sent += pending
      try {
        const p = report(node, s)
        if (p && typeof p.catch === 'function') p.catch(() => {})
      } catch { /* a lost report is only a little less known */ }
    }
    pending = 0
  }

  return {
    show(id) {
      if (id === node) return
      flush()
      node = id || null; sent = 0; pending = 0
      since = node && shown ? now() : null
    },
    visible(on) {
      if (on === shown) return
      if (!on) { flush(); shown = false; since = null } else { shown = true; since = node ? now() : null }
    },
    tick() { accrue(); if (pending >= DWELL_EVERY_S) flush() },
    flush,
    leave() { flush(); node = null; since = null; sent = 0 },
  }
}
