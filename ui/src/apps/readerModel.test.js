import { describe, expect, it, vi } from 'vitest'
import {
  ANSWER_CHARS, READER_RESET_EVENT, announceReaderReset, answerCheck, dueBadge, dueLabel, dwellTracker, emptyReviewText,
  formatDuration, hostedNote, levelBars, onReaderReset,
  markLabel, questErrorText, questProgress, questRoomView, quizErrorText, shareBars, summaryLine, testMeButton, topicBars,
} from './readerModel.js'

const DAY = 86400

describe('shareBars', () => {
  it('gives the three branch styles as percentages in a fixed order', () => {
    const bars = shareBars({ context: 0.5, general: 0.25, sideways: 0.25, branches: 8, lean: 'context' })
    expect(bars.map(b => b.key)).toEqual(['context', 'general', 'sideways'])
    expect(bars.map(b => b.pct)).toEqual([50, 25, 25])
    expect(bars[0]).toMatchObject({ label: 'In this context', share: 0.5, lean: true })
    expect(bars[1].lean).toBe(false)
  })
  it('is empty with no branch opens, and clamps junk', () => {
    expect(shareBars({ context: 0, general: 0, sideways: 0, branches: 0, lean: 'even' })).toEqual([])
    expect(shareBars(null)).toEqual([])
    const bars = shareBars({ context: 2, general: NaN, sideways: -1, branches: 3 })
    expect(bars.map(b => b.share)).toEqual([1, 0, 0])
  })
})

describe('levelBars', () => {
  it('shows deeper and simpler against their total', () => {
    const l = levelBars({ deeper: 3, simpler: 1, lean: 'deeper' })
    expect(l.bars).toEqual([
      { key: 'deeper', label: 'Deeper', n: 3, share: 0.75, lean: true },
      { key: 'simpler', label: 'Simpler', n: 1, share: 0.25, lean: false },
    ])
    expect(l.text).toBe('Asks for deeper pages more often')
  })
  it('says nothing was asked yet, and even', () => {
    expect(levelBars({ deeper: 0, simpler: 0, lean: 'even' }).text).toBe('No Deeper or Simpler asks yet')
    expect(levelBars({ deeper: 1, simpler: 1, lean: 'even' }).text).toBe('No clear lean')
    expect(levelBars(undefined).bars.map(b => b.n)).toEqual([0, 0])
  })
})

describe('topicBars and formatDuration', () => {
  it('scales opens to the most-read topic and shows time', () => {
    const t = topicBars([{ title: 'Castles', opens: 4, seconds: 125 }, { title: 'Siege', opens: 2, seconds: 30 }])
    expect(t).toEqual([
      { title: 'Castles', opens: 4, share: 1, time: '2 min' },
      { title: 'Siege', opens: 2, share: 0.5, time: '30 s' },
    ])
    expect(topicBars([])).toEqual([])
    // a topic with dwell but no opens still shows
    expect(topicBars([{ title: 'A', opens: 0, seconds: 0 }])[0].share).toBe(0)
  })
  it('formats durations', () => {
    expect(formatDuration(0)).toBe('')
    expect(formatDuration(59)).toBe('59 s')
    expect(formatDuration(60)).toBe('1 min')
    expect(formatDuration(3600 + 120)).toBe('1 h 2 min')
    expect(formatDuration(7200)).toBe('2 h')
  })
})

describe('dueLabel', () => {
  const now = 1_000_000 * 1000           // ms
  const at = (s) => ({ due_at: now / 1000 + s })
  it('says when an item comes back', () => {
    expect(dueLabel(at(DAY), now)).toBe('next due in 1 day')
    expect(dueLabel(at(3 * DAY), now)).toBe('next due in 3 days')
    expect(dueLabel(at(2.9 * DAY), now)).toBe('next due in 3 days')
    expect(dueLabel(at(5 * 3600), now)).toBe('next due in 5 hours')
    expect(dueLabel(at(1800), now)).toBe('next due in under an hour')
    expect(dueLabel(at(-5), now)).toBe('due now')
    expect(dueLabel(at(0), now)).toBe('due now')
  })
  it('is empty without a due time', () => {
    expect(dueLabel({}, now)).toBe('')
    expect(dueLabel(null, now)).toBe('')
  })
})

describe('emptyReviewText', () => {
  const now = 2_000_000_000
  it('names the soonest due question when known', () => {
    expect(emptyReviewText({ due: 0, total: 4 }, now / 1000 + 2 * DAY, now)).toBe('Nothing due — next in 2 days')
  })
  it('uses the counts\' next_due_at, or the sooner of the two', () => {
    expect(emptyReviewText({ due: 0, total: 4, next_due_at: now / 1000 + 2 * DAY }, null, now)).toBe('Nothing due — next in 2 days')
    expect(emptyReviewText({ due: 0, total: 4, next_due_at: now / 1000 + 5 * DAY }, now / 1000 + DAY, now)).toBe('Nothing due — next in 1 day')
    expect(emptyReviewText({ due: 0, total: 4, next_due_at: null }, null, now)).toBe('Nothing due')
  })
  it('falls back to plain words', () => {
    expect(emptyReviewText({ due: 0, total: 4 }, null, now)).toBe('Nothing due')
    expect(emptyReviewText({ due: 0, total: 0 }, null, now)).toBe('Nothing due — use Test me on a page to make some questions')
  })
})

describe('hostedNote', () => {
  it('says which hosted model gets what', () => {
    expect(hostedNote({ hosted: false, hosted_models: [] })).toBe('')
    expect(hostedNote({ hosted: true, hosted_models: ['writing'] })).toBe('This profile goes to the hosted writing model with every page request.')
    expect(hostedNote({ hosted: true, hosted_models: ['fast'] })).toBe('Page titles, counts and quiz results go to the hosted fast model to write the summary.')
    expect(hostedNote({ hosted: true, hosted_models: ['writing', 'fast'] })).toContain('writing model with every page request. Page titles')
    expect(hostedNote({ hosted: true })).toContain('hosted writing model')   // an older server
  })
})

describe('the reader reset event', () => {
  it('reaches listeners until they unsubscribe', () => {
    vi.stubGlobal('window', new EventTarget())   // (node: no DOM)
    const fn = vi.fn()
    const off = onReaderReset(fn)
    announceReaderReset()
    window.dispatchEvent(new Event(READER_RESET_EVENT))
    off()
    announceReaderReset()
    expect(fn).toHaveBeenCalledTimes(2)
    vi.unstubAllGlobals()
  })
})

describe('markLabel', () => {
  it('maps the three marks to words and tones', () => {
    expect(markLabel('right')).toEqual({ text: 'Right', tone: 'ok' })
    expect(markLabel('partly')).toEqual({ text: 'Partly', tone: 'warm' })
    expect(markLabel('not_yet')).toEqual({ text: 'Not yet', tone: 'warn' })
    expect(markLabel('weird')).toEqual({ text: 'weird', tone: 'neutral' })
    expect(markLabel(null)).toEqual({ text: '', tone: 'neutral' })
  })
})

describe('answerCheck', () => {
  it('counts and refuses empty or over-long answers', () => {
    expect(ANSWER_CHARS).toBe(2000)
    expect(answerCheck('')).toEqual({ ok: false, count: '0/2000', reason: 'Type an answer first' })
    expect(answerCheck('   ').ok).toBe(false)
    expect(answerCheck('a cat')).toEqual({ ok: true, count: '5/2000', reason: '' })
    const long = answerCheck('x'.repeat(2001))
    expect(long.ok).toBe(false)
    expect(long.reason).toBe('At most 2000 characters')
  })
})

describe('quizErrorText', () => {
  const err = (status, message) => Object.assign(new Error(message), { status })
  it('says what each failure means', () => {
    expect(quizErrorText(err(409, 'x'))).toBe('Reset happened; try again')
    expect(quizErrorText(err(502, 'x'))).toBe("The model's reply couldn't be read; try again")
    expect(quizErrorText(err(503, 'no writing model in this tier'))).toBe('no writing model in this tier')
    expect(quizErrorText(err(404, 'page not found'))).toBe('page not found')
    expect(quizErrorText(new Error('Failed to fetch'))).toBe('Failed to fetch')
  })
})

describe('questErrorText', () => {
  it('keeps the server words for a 409 (building, moved on), names a 502', () => {
    const err = (status, message) => Object.assign(new Error(message), { status })
    expect(questErrorText(err(409, 'a quest is being built'))).toBe('a quest is being built')
    expect(questErrorText(err(400, 'read at least two pages first'))).toBe('read at least two pages first')
    expect(questErrorText(err(502, 'x'))).toBe("The model's reply couldn't be read; try again")
  })
})

describe('testMeButton', () => {
  it('needs a written page and a writing model', () => {
    const st = (state, reason = '') => ({ servers: [{ role: 'text', state, reason }] })
    expect(testMeButton(st('ready'), { nodeId: 'n', streaming: false })).toEqual({ enabled: true, reason: '' })
    expect(testMeButton(st('external'), { nodeId: 'n' }).enabled).toBe(true)
    expect(testMeButton(st('busy'), { nodeId: 'n' }).enabled).toBe(true)
    expect(testMeButton(st('ready'), { nodeId: null })).toEqual({ enabled: false, reason: 'Open a page first' })
    expect(testMeButton(st('ready'), { nodeId: 'n', streaming: true })).toEqual({ enabled: false, reason: 'Wait for the page to finish' })
    expect(testMeButton(st('failed', 'exited'), { nodeId: 'n' })).toEqual({ enabled: false, reason: 'The writing model is unavailable: exited' })
    expect(testMeButton(st('unavailable'), { nodeId: 'n' })).toEqual({ enabled: false, reason: 'The writing model is unavailable' })
    expect(testMeButton({ servers: [] }, { nodeId: 'n' }).enabled).toBe(false)
    // status not known yet (or the check failed): the server will say
    expect(testMeButton(null, { nodeId: 'n' }).enabled).toBe(true)
  })
})

describe('summaryLine', () => {
  const base = { summary: '', error: '', writing: false, fast: { ok: true, reason: '' }, counted: { opens: 0 } }
  it('shows the summary when there is one', () => {
    expect(summaryLine({ ...base, summary: 'Likes castles.' })).toEqual({ text: 'Likes castles.', dim: false })
  })
  it('says why there is none', () => {
    expect(summaryLine(base)).toEqual({ text: 'Not enough reading yet', dim: true })
    expect(summaryLine({ ...base, fast: { ok: false, reason: 'no fast model in this tier' }, counted: { opens: 9 } }))
      .toEqual({ text: 'Not written yet: no fast model in this tier', dim: true })
    expect(summaryLine({ ...base, error: 'boom', counted: { opens: 9 } })).toEqual({ text: 'Not written yet: boom', dim: true })
    expect(summaryLine({ ...base, counted: { opens: 4 } }).text).toBe('Not written yet: it is written every few pages, or press Refresh')
    expect(summaryLine({ ...base, writing: true, counted: { opens: 4 } })).toEqual({ text: 'Writing…', dim: true })
  })
})

describe('dueBadge', () => {
  it('is a short count or nothing', () => {
    expect(dueBadge(0)).toBe('')
    expect(dueBadge(undefined)).toBe('')
    expect(dueBadge(7)).toBe('7')
    expect(dueBadge(120)).toBe('99+')
  })
})

describe('questRoomView', () => {
  it('shows a passed room with its answer', () => {
    const r = { index: 0, locked: false, title: 'Trebuchet', description: 'A hall.', question: 'Q?', opened: true, skipped: false, tries: 1, answer: 'A.', hint: 'H.' }
    expect(questRoomView(r)).toEqual({ kind: 'passed', index: 0, status: 'opened', title: 'Trebuchet', description: 'A hall.', question: 'Q?', answer: 'A.' })
    expect(questRoomView({ ...r, opened: false, skipped: true }).status).toBe('skipped')
  })
  it('hides a locked room', () => {
    expect(questRoomView({ index: 3, locked: true })).toEqual({ kind: 'locked', index: 3 })
  })
  it('unfolds the current room by tries', () => {
    const r = { index: 1, locked: false, node_id: 'n1', title: 'T', description: 'D', question: 'Q', opened: false, skipped: false, tries: 0 }
    expect(questRoomView(r)).toEqual({ kind: 'current', index: 1, nodeId: 'n1', title: 'T', description: 'D', question: 'Q', tries: 0, hint: '', reread: false, canSkip: false })
    expect(questRoomView({ ...r, tries: 1, hint: 'Look up.' })).toMatchObject({ hint: 'Look up.', reread: false, canSkip: false })
    expect(questRoomView({ ...r, tries: 2, hint: 'h' })).toMatchObject({ reread: true, canSkip: false })
    expect(questRoomView({ ...r, tries: 3, hint: 'h' })).toMatchObject({ reread: true, canSkip: true })
  })
})

describe('questProgress', () => {
  it('words the build', () => {
    expect(questProgress({ status: 'building', building: { done: 2, total: 5 } })).toBe('Building the rooms… 2 of 5')
    expect(questProgress({ status: 'building' })).toBe('Building the rooms…')
    expect(questProgress({ status: 'active' })).toBe('')
  })
})

describe('dwellTracker', () => {
  const setup = () => {
    let t = 0
    const report = vi.fn()
    const d = dwellTracker({ report, now: () => t })
    return { d, report, at: (s) => { t = s * 1000 } }
  }

  it('reports visible seconds every 30 s', () => {
    const { d, report, at } = setup()
    d.show('a')
    at(10); d.tick()
    expect(report).not.toHaveBeenCalled()
    at(31); d.tick()
    expect(report).toHaveBeenCalledWith('a', 31)
    at(40); d.tick()
    expect(report).toHaveBeenCalledTimes(1)
  })

  it('flushes on node change and on leave', () => {
    const { d, report, at } = setup()
    d.show('a'); at(12); d.show('b')
    expect(report).toHaveBeenLastCalledWith('a', 12)
    at(20); d.leave()
    expect(report).toHaveBeenLastCalledWith('b', 8)
    at(100); d.tick(); d.leave()
    expect(report).toHaveBeenCalledTimes(2)
  })

  it('does not count hidden time, and flushes when hidden', () => {
    const { d, report, at } = setup()
    d.show('a'); at(5); d.visible(false)
    expect(report).toHaveBeenLastCalledWith('a', 5)
    at(500); d.visible(true)
    at(510); d.leave()
    expect(report).toHaveBeenLastCalledWith('a', 10)
  })

  it('never reports more than 600 s for one visit', () => {
    const { d, report, at } = setup()
    d.show('a')
    for (let s = 30; s <= 2000; s += 30) { at(s); d.tick() }
    d.leave()
    const sum = report.mock.calls.reduce((n, c) => n + c[1], 0)
    expect(sum).toBe(600)
    // a new visit (another node, then back) may count again
    const { d: d2, report: r2, at: at2 } = setup()
    d2.show('a'); at2(700); d2.show('b'); d2.show('a'); at2(730); d2.leave()
    expect(r2.mock.calls).toEqual([['a', 600], ['a', 30]])
  })

  it('reopening the same node keeps the visit', () => {
    const { d, report, at } = setup()
    d.show('a'); at(590); d.show('a'); at(700); d.leave()
    const sum = report.mock.calls.reduce((n, c) => n + c[1], 0)
    expect(sum).toBe(600)
  })

  it('flush (the tab going away) reports what piled up but keeps the visit', () => {
    const { d, report, at } = setup()
    d.show('a'); at(12); d.flush()
    expect(report).toHaveBeenLastCalledWith('a', 12)
    at(20); d.tick(); d.leave()   // back from the cache: still the same visit, same node
    expect(report).toHaveBeenLastCalledWith('a', 8)
    const { d: d2, report: r2, at: at2 } = setup()
    d2.show('a'); at2(590); d2.flush(); at2(700); d2.leave()
    expect(r2.mock.calls.reduce((n, c) => n + c[1], 0)).toBe(600)   // the cap still holds
  })

  it('skips under a second, nothing without a node, and survives a failing report', () => {
    let t = 0
    const report = vi.fn(() => { throw new Error('down') })
    const d = dwellTracker({ report, now: () => t })
    d.show(null); t = 50000; d.leave()
    expect(report).not.toHaveBeenCalled()
    d.show('a'); t = 50500; d.leave()
    expect(report).not.toHaveBeenCalled()
    d.show('a'); t = 60000
    expect(() => d.leave()).not.toThrow()
    const rejecting = dwellTracker({ report: () => Promise.reject(new Error('x')), now: () => t })
    rejecting.show('a'); t = 90000
    expect(() => rejecting.leave()).not.toThrow()
  })
})
