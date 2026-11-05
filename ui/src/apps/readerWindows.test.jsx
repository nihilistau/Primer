/* the Reader (model) and Quest windows render their states (server-side render: no DOM) */
import { describe, expect, test } from 'vitest'
import { renderToString } from 'react-dom/server'
import { Model } from './ReaderWindow.jsx'
import { Run } from './QuestWindow.jsx'
import QuestionCard from '../reader/QuestionCard.jsx'

const flat = (el) => renderToString(el).replace(/<[^>]+>/g, ' ').replace(/&#x27;/g, "'").replace(/\s+/g, ' ')

const counted = {
  opens: 9, level: { deeper: 3, simpler: 1, lean: 'deeper' },
  style: { context: 0.5, general: 0.25, sideways: 0.25, branches: 8, lean: 'context' },
  topics: [{ title: 'Castles', opens: 4, seconds: 125 }],
}
const reader = {
  summary: '', notes: 'I am 12', enabled: true, updated_at: null, generation: 0, error: '', counted,
  knows: ['Trebuchet'], hard: ['Siege towers'], writing: false, fast: { ok: false, reason: 'no fast model in this tier' }, hosted: true,
}

describe('Reader window', () => {
  test('says why there is no summary, shows bars, lists, notes, the switch and the hosted note', () => {
    const t = flat(<Model st={reader} refresh={() => {}} />)
    expect(t).toContain('Not written yet: no fast model in this tier')
    expect(t).toContain('Asks for deeper pages more often')
    expect(t).toContain('In this context')
    expect(t).toContain('50%')
    expect(t).toContain('Castles')
    expect(t).toContain('2 min')
    expect(t).toContain('Trebuchet')
    expect(t).toContain('Siege towers')
    expect(t).toContain('I am 12')
    expect(t).toContain('7/1000')
    expect(t).toContain('Use this in pages')
    expect(t).toContain('This profile goes to the hosted writing model with every page request.')
    expect(t).toContain('Reset')
  })
  test('writing shows the busy chip; local has no hosted note', () => {
    const t = flat(<Model st={{ ...reader, writing: true, hosted: false, summary: 'Likes castles.' }} refresh={() => {}} />)
    expect(t).toContain('Likes castles.')
    expect(t).toContain('writing…')
    expect(t).not.toContain('hosted')
  })
})

const room = (i, extra) => ({ index: i, locked: false, node_id: 'n' + i, title: 'Page ' + i, description: 'A hall ' + i + '.',
  question: 'Door ' + i + '?', opened: false, skipped: false, tries: 0, ...extra })

describe('Quest window', () => {
  test('no quest: build from recent reading (and from this page when given)', () => {
    const t = flat(<Run q={null} refresh={() => {}} fromNodeId="n1" />)
    expect(t).toContain('Build a quest')
    expect(t).toContain('Build from this page’s trail')
  })
  test('active: Leave this quest, the trail build and the hint; finished: no Leave', () => {
    const q = { id: 'q', status: 'active', at_room: 0, tries: 1, rooms: [room(0, { tries: 1, hint: 'Look at the sling.' })] }
    const t = flat(<Run q={q} refresh={() => {}} fromNodeId="n1" />)
    expect(t).toContain('Leave this quest')
    expect(t).toContain('Build from this page’s trail')
    expect(t).toContain('Look at the sling.')
    const done = { id: 'q', status: 'finished', at_room: 1, rooms: [room(0, { opened: true, answer: 'a' })], summary: { opened: 1, skipped: 0, rooms: 1 } }
    expect(flat(<Run q={done} refresh={() => {}} />)).not.toContain('Leave this quest')
  })
  test('building shows progress', () => {
    expect(flat(<Run q={{ id: 'q', status: 'building', building: { done: 2, total: 5 }, rooms: [] }} refresh={() => {}} />))
      .toContain('Building the rooms… 2 of 5')
  })
  test('active: passed rooms with answers, the current door with its hint and skip, later rooms locked', () => {
    const q = { id: 'q', status: 'active', at_room: 1, tries: 3, can_skip: true, rooms: [
      room(0, { opened: true, answer: 'Answer 0', hint: 'h0', tries: 1 }),
      room(1, { tries: 3, hint: 'Look at the counterweight.' }),
      { index: 2, locked: true },
    ] }
    const t = flat(<Run q={q} refresh={() => {}} />)
    expect(t).toContain('Answer 0')
    expect(t).toContain('Door 1?')
    expect(t).toContain('Look at the counterweight.')
    expect(t).toContain('Reread the page: the answer is in it.')
    expect(t).toContain('Skip this room')
    expect(t).toContain('locked')
    expect(t).not.toContain('Door 2')
  })
  test('finished: the summary and Build another', () => {
    const q = { id: 'q', status: 'finished', at_room: 2, rooms: [room(0, { opened: true, answer: 'a' }), room(1, { skipped: true, answer: 'b' })],
                summary: { opened: 1, skipped: 1, rooms: 2 } }
    const t = flat(<Run q={q} refresh={() => {}} />)
    expect(t).toContain('1 of 2 doors opened, 1 skipped.')
    expect(t).toContain('Build another')
  })
  test('failed: the reason', () => {
    expect(flat(<Run q={{ id: 'q', status: 'failed', error: 'read at least two pages first', rooms: [] }} refresh={() => {}} />))
      .toContain('read at least two pages first')
  })
})

describe('QuestionCard', () => {
  test('a question with its counter, Check and Show answer, and its page link in Review', () => {
    const item = { id: 'i', node_id: 'n', title: 'Trebuchet', kind: 'why', question: 'Why the counterweight?', last_mark: 'partly' }
    const t = flat(<QuestionCard item={item} onOpenPage={() => {}} showDue />)
    expect(t).toContain('Explain why')
    expect(t).toContain('Trebuchet')
    expect(t).toContain('Why the counterweight?')
    expect(t).toContain('0/2000')
    expect(t).toContain('Check')
    expect(t).toContain('Show answer')
    expect(t).toContain('last time: partly')
  })
})
