/* the Tale window renders its states (server-side render: no DOM) */
import { describe, expect, test } from 'vitest'
import { renderToString } from 'react-dom/server'
import { Tale } from './TaleWindow.jsx'

const flat = (el) => renderToString(el).replace(/<[^>]+>/g, ' ').replace(/&#x27;/g, "'").replace(/&lt;/g, '<').replace(/&gt;/g, '>')
  .replace(/\s+/g, ' ')

const bible = {
  hero: { name: 'Nell', gender: 'female', description: 'A girl who builds things.', look: 'red coat, brass goggles' },
  companions: [{ name: 'Harv', look: 'tall', note: 'a friend' }], places: ['the mill'], things: [],
  threads: [{ text: 'who made the key?', open: true }], chapters: ['They met.'],
}
const current = {
  id: 't1', title: 'Nell and the Engine', seed: 'Nell', bible, writing: false, updating: false,
  chapters: [
    { n: 1, story_id: 's1', title: 'Chapter 1: The mill', status: 'done', pages: [{ node_id: 'a', title: 'Windmills' }],
      summary: 'Nell found <b>a key</b>.', update_failed: false },
    { n: 2, story_id: 's2', title: 'Chapter 2: The key', status: 'narrating', pages: [], summary: '', update_failed: true },
  ],
}
const state = (cur, extra = {}) => ({
  current: cur, tales: [{ id: 't1', title: 'Nell and the Engine', chapters: 2 }, { id: 't0', title: 'Old one', chapters: 1 }],
  preview: { pages: [{ node_id: 'b', title: 'Gears', seconds: 40, opens: 2 }], waiting: 3 }, ...extra,
})

describe('Tale window', () => {
  test('no tale: the seed box, Start and Invent, and the older tales', () => {
    const t = flat(<Tale state={{ current: null, tales: [{ id: 't0', title: 'Old one', chapters: 1 }], preview: { pages: [], waiting: 0 } }} />)
    expect(t).toContain('Who is the story about?')
    expect(t).toContain('0 / 500')
    expect(t).toContain('Start the tale')
    expect(t).toContain('Invent one for me')
    expect(t).toContain('Older tales')
    expect(t).toContain('Old one')
  })
  test('a tale: next chapter, chapters, the bible and older tales; model text as text', () => {
    const html = renderToString(<Tale state={state(current)} />)
    const t = flat(<Tale state={state(current)} />)
    expect(t).toContain('Nell and the Engine')
    expect(t).toContain('3 new pages waiting: time for the next chapter')
    expect(t).toContain('Gears')
    expect(t).toContain('Add a page from your trail')
    expect(t).toContain('Chapter 1 · The mill · ready')
    expect(t).toContain('Chapter 2 · The key · being narrated')
    expect(t).toContain('Windmills')
    expect(html).not.toContain('<b>a key</b>')
    expect(t).toContain('Nell found <b>a key</b>.')
    expect(t).toContain('The story bible wasn’t updated')
    expect(t).toContain('Retry')
    expect(t).toContain('red coat, brass goggles')
    expect(t).toContain('Nell')
    expect(t).toContain('Female')
    expect(t).not.toContain('she/her')
    expect(t).toContain('Harv')
    expect(t).toContain('tall · a friend')
    expect(t).toContain('who made the key?')
    expect(t).toContain('Start a new tale')
    expect(t).toContain('Old one')
    expect(t).not.toContain('Writing chapter')
    expect(html).toContain('aria-label="Retry chapter 2"')
    expect(html).toContain('aria-label="Open Old one"')
    expect(html).toContain('aria-label="Remove the mill"')
  })
  test('while writing, the bible can’t be changed and says why; a nameless hero shows the tale title', () => {
    const cur = { ...current, writing: true, bible: { ...bible, hero: { ...bible.hero, name: '' } } }
    const html = renderToString(<Tale state={state(cur)} />)
    expect(flat(<Tale state={state(cur)} />)).toMatch(/A chapter is being written ?; places and threads can be removed when it’s done\./)
    expect(html).toMatch(/<button[^>]*disabled=""[^>]*aria-label="Remove the mill"/)
    expect(html).toContain('<div class="tl-hero-name">Nell and the Engine</div>')
  })
  test('writing and updating say so; Next chapter waits', () => {
    expect(flat(<Tale state={state({ ...current, writing: true })} />)).toContain('Writing chapter 3…')
    expect(flat(<Tale state={state({ ...current, updating: true })} />)).toContain('Updating the story bible…')
  })
  test('nothing to draw on: the reason shows', () => {
    const t = flat(<Tale state={state(current, { preview: { pages: [], waiting: 0 } })} />)
    expect(t).toContain('No new pages since the last chapter')
    expect(t).toContain('Read a page or two first, or add one from your trail')
  })
})
