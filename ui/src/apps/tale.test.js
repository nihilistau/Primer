import { describe, expect, it } from 'vitest'
import {
  MAX_PAGES, SEED_MAX, bibleLock, bibleRows, heroName, storyChapter, canNext, chapterHeading, chapterLabel, defaultChosen, findChapter, previewLabel,
  progressLabel, seedCheck, taleErrorText, trailPages, waitingMark, waitingMarkLabel, pollMs, POLL_BUSY_MS, POLL_TELLING_MS,
  heroGender,
} from './tale.js'

describe('previewLabel', () => {
  it('says how many unused pages wait', () => {
    expect(previewLabel({ pages: [], waiting: 0 })).toBe('No new pages since the last chapter')
    expect(previewLabel({ pages: [], waiting: 1 })).toBe('1 new page waiting')
    expect(previewLabel({ pages: [], waiting: 2 })).toBe('2 new pages waiting')
    expect(previewLabel({ pages: [], waiting: 4 })).toBe('4 new pages waiting: time for the next chapter')
  })
  it('copes with no preview', () => {
    expect(previewLabel(null)).toBe('No new pages since the last chapter')
    expect(previewLabel({})).toBe('No new pages since the last chapter')
  })
})

describe('waitingMark', () => {
  it('marks the dock from three waiting pages', () => {
    expect(waitingMark({ waiting: 2 })).toBe(0)
    expect(waitingMark({ waiting: 3 })).toBe(3)
    expect(waitingMark({ waiting: 7 })).toBe(7)
    expect(waitingMark(null)).toBe(0)
    expect(waitingMark({ waiting: 'x' })).toBe(0)
  })
  it('labels the mark', () => {
    expect(waitingMarkLabel(3)).toBe('3 pages waiting for the next chapter')
  })
})

describe('chapterLabel', () => {
  it('is Chapter N · title · status, without the title repeating the number', () => {
    expect(chapterLabel({ n: 2, title: 'Chapter 2: The bridge', status: 'done' })).toBe('Chapter 2 · The bridge · ready')
    expect(chapterLabel({ n: 1, title: 'A start', status: 'narrating' })).toBe('Chapter 1 · A start · being narrated')
    expect(chapterLabel({ n: 3, title: 'Chapter 3', status: 'writing' })).toBe('Chapter 3 · being written')
    expect(chapterLabel({ n: 4, title: 'X', status: 'failed' })).toBe('Chapter 4 · X · failed')
    expect(chapterLabel({ n: 5, title: 'Y', status: 'odd' })).toBe('Chapter 5 · Y · odd')
  })
})

describe('chapterHeading', () => {
  it('names the chapter and its tale when known', () => {
    expect(chapterHeading({ n: 2, taleTitle: 'Nell and the Engine' }, 'Chapter 2: The bridge'))
      .toBe('Chapter 2 of Nell and the Engine')
  })
  it('falls back to Chapter + the story title', () => {
    expect(chapterHeading(null, 'Chapter 2: The bridge')).toBe('Chapter 2: The bridge')
    expect(chapterHeading(null, 'The bridge')).toBe('Chapter: The bridge')
    expect(chapterHeading(null, '')).toBe('Chapter')
    expect(chapterHeading({ n: 2 }, 'The bridge')).toBe('Chapter 2: The bridge')
  })
})

describe('findChapter', () => {
  it('finds a story among the current tale’s chapters', () => {
    const state = { current: { title: 'T', chapters: [{ n: 1, story_id: 'a' }, { n: 2, story_id: 'b' }] } }
    expect(findChapter(state, 'b')).toEqual({ n: 2, taleTitle: 'T' })
    expect(findChapter(state, 'z')).toBe(null)
    expect(findChapter({ current: null }, 'b')).toBe(null)
    expect(findChapter(null, 'b')).toBe(null)
  })
})

describe('bibleRows', () => {
  const bible = {
    hero: { name: 'Nell', description: 'builds things', look: 'red coat' },
    companions: [{ name: 'Harv', look: 'tall', note: 'a friend' }, { name: 'Dot', look: '', note: '' }],
    places: ['the mill'],
    things: [],
    threads: [{ text: 'who made the key?', open: true }, { text: 'the lost dog', open: false }],
    chapters: ['s1'],
  }
  it('lists the sections in order, with what can be removed', () => {
    const rows = bibleRows(bible)
    expect(rows.map(r => r.key)).toEqual(['companions', 'places', 'things', 'threads'])
    expect(rows.map(r => r.label)).toEqual(['Companions', 'Places', 'Things carried', 'Threads'])
    expect(rows[0].items).toEqual([
      { text: 'Harv', detail: 'tall · a friend' },
      { text: 'Dot', detail: '' },
    ])
    expect(rows[1].items).toEqual([{ text: 'the mill', detail: '', remove: { remove_place: 'the mill' } }])
    expect(rows[2].items).toEqual([])
    expect(rows[3].items).toEqual([
      { text: 'who made the key?', detail: 'open', remove: { remove_thread: 'who made the key?' } },
      { text: 'the lost dog', detail: 'closed', remove: { remove_thread: 'the lost dog' } },
    ])
  })
  it('copes with a missing or malformed bible', () => {
    expect(bibleRows(null).every(r => r.items.length === 0)).toBe(true)
    expect(bibleRows({ places: 'x', companions: [null, 5], threads: [{}] }).map(r => r.items.length)).toEqual([0, 0, 0, 0])
  })
})

describe('canNext', () => {
  const st = (current) => ({ current, tales: [], preview: { pages: [], waiting: 0 } })
  const cur = { id: 't', chapters: [], writing: false, updating: false }
  it('needs a tale', () => {
    expect(canNext(st(null), ['a'])).toEqual({ ok: false, reason: 'Start a tale first' })
    expect(canNext(null, ['a'])).toEqual({ ok: false, reason: 'Start a tale first' })
  })
  it('waits for a chapter being written or the bible being updated', () => {
    expect(canNext(st({ ...cur, writing: true }), ['a'])).toEqual({ ok: false, reason: 'A chapter is being written' })
    expect(canNext(st({ ...cur, updating: true }), ['a'])).toEqual({ ok: false, reason: 'The story bible is being updated' })
  })
  it('needs a page, and at most three', () => {
    expect(canNext(st(cur), [])).toEqual({ ok: false, reason: 'Read a page or two first, or add one from your trail' })
    expect(canNext(st(cur), ['a', 'b', 'c', 'd'])).toEqual({ ok: false, reason: `Choose up to ${MAX_PAGES} pages` })
    expect(canNext(st(cur), ['a', 'b', 'c'])).toEqual({ ok: true, reason: '' })
  })
})

describe('progressLabel', () => {
  it('says what is going on', () => {
    expect(progressLabel({ writing: true, updating: false, chapters: [{}, {}] })).toBe('Writing chapter 3…')
    expect(progressLabel({ writing: false, updating: true, chapters: [{}] })).toBe('Updating the story bible…')
    expect(progressLabel({ writing: false, updating: false, chapters: [] })).toBe('')
    expect(progressLabel(null)).toBe('')
  })
})

describe('defaultChosen', () => {
  it('ticks every suggested page', () => {
    expect(defaultChosen({ pages: [{ node_id: 'a' }, { node_id: 'b' }] })).toEqual(['a', 'b'])
    expect(defaultChosen(null)).toEqual([])
  })
})

describe('seedCheck', () => {
  it('counts, and needs words within the limit', () => {
    expect(seedCheck('')).toEqual({ ok: false, count: `0 / ${SEED_MAX}`, reason: 'Say who the story is about, or let Primer invent one' })
    expect(seedCheck('   ')).toMatchObject({ ok: false })
    expect(seedCheck('a girl called Nell')).toEqual({ ok: true, count: `18 / ${SEED_MAX}`, reason: '' })
    expect(seedCheck('x'.repeat(SEED_MAX + 1))).toMatchObject({ ok: false, reason: `At most ${SEED_MAX} characters` })
  })
})

describe('trailPages', () => {
  const graph = {
    nodes: [
      { id: 'a', title: 'Apples', concept: 'apples' },
      { id: 'b', title: '', concept: 'bees' },
      { id: 'c', title: 'Clocks', concept: 'clocks' },
    ],
    edges: [
      { from_id: null, to_id: 'a', at: 1 },
      { from_id: 'a', to_id: 'b', at: 3 },
      { from_id: 'b', to_id: 'a', at: 5 },
      { from_id: 'a', to_id: 'c', at: 2 },
      { from_id: 'a', to_id: 'gone', at: 9 },
    ],
  }
  it('gives recently arrived pages, newest first, once each, skipping some', () => {
    expect(trailPages(graph, ['c'])).toEqual([
      { node_id: 'a', title: 'Apples' },
      { node_id: 'b', title: 'bees' },
    ])
    expect(trailPages(graph, [], 1)).toEqual([{ node_id: 'a', title: 'Apples' }])
    expect(trailPages(null)).toEqual([])
  })
})

describe('taleErrorText', () => {
  it('keeps the server’s words and renames an unreadable reply', () => {
    expect(taleErrorText(Object.assign(new Error('read a page or two first'), { status: 400 }))).toBe('read a page or two first')
    expect(taleErrorText(Object.assign(new Error('x'), { status: 502 }))).toBe("The model's reply couldn't be read; try again")
    expect(taleErrorText('boom')).toBe('boom')
  })
})

describe('bibleLock', () => {
  it('says why the bible can’t be changed now', () => {
    expect(bibleLock({ writing: true })).toBe('A chapter is being written')
    expect(bibleLock({ updating: true })).toBe('The story bible is being updated')
    expect(bibleLock({ writing: false, updating: false })).toBe('')
    expect(bibleLock(null)).toBe('')
  })
})

describe('heroName', () => {
  it('is the hero’s name, else the tale’s title', () => {
    expect(heroName({ title: 'T', bible: { hero: { name: 'Nell' } } })).toBe('Nell')
    expect(heroName({ title: 'T', bible: { hero: { name: '  ' } } })).toBe('T')
    expect(heroName({ title: 'T', bible: {} })).toBe('T')
    expect(heroName(null)).toBe('')
  })
})

describe('storyChapter', () => {
  it('reads a story’s tale field as chapter info', () => {
    expect(storyChapter({ kind: 'chapter', tale: { id: 't', title: 'T', n: 2 } })).toEqual({ n: 2, taleTitle: 'T' })
    expect(storyChapter({ kind: 'chapter', tale: null })).toBe(null)
    expect(storyChapter({ kind: 'chapter' })).toBe(null)
    expect(storyChapter(null)).toBe(null)
  })
})

describe('pollMs and heroGender', () => {
  it('polls quickly while writing, slowly while a chapter is told, else not', () => {
    expect(pollMs(null)).toBe(0)
    expect(pollMs({ writing: true, chapters: [] })).toBe(POLL_BUSY_MS)
    expect(pollMs({ updating: true, chapters: [{ status: 'done' }] })).toBe(POLL_BUSY_MS)
    expect(pollMs({ chapters: [{ status: 'done' }, { status: 'narrating' }] })).toBe(POLL_TELLING_MS)
    expect(pollMs({ chapters: [{ status: 'writing' }] })).toBe(POLL_TELLING_MS)
    expect(pollMs({ chapters: [{ status: 'done' }, { status: 'failed' }, null] })).toBe(0)
    expect(POLL_TELLING_MS).toBeGreaterThanOrEqual(4000)
  })
  it('shows the hero’s gender in plain words', () => {
    expect(heroGender({ bible: { hero: { name: 'Wren', gender: 'female' } } })).toBe('Female')
    expect(heroGender({ bible: { hero: { name: 'Bram', gender: 'male' } } })).toBe('Male')
    expect(heroGender({ bible: { hero: { name: 'Wren', gender: '' } } })).toBe('')
    expect(heroGender(null)).toBe('')
  })
})
