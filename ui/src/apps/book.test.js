import { describe, expect, it } from 'vitest'
import {
  allIncluded, bookDate, bookErrorText, defaultTitle, formatSize, isActive, pickablePages, previewText,
  progressText, scopeBody, sinceSeconds, sizesText,
} from './book.js'

const snap = {
  nodes: [
    { id: 'n3', kind: 'concept', concept: 'trebuchet', title: 'Trebuchet', parent_id: null },
    { id: 'n1', kind: 'concept', concept: 'castle', title: 'castle', parent_id: null },
    { id: 'n2', kind: 'concept', concept: 'Castle', title: 'Castle', parent_id: null },
    { id: 'n4', kind: 'phrase', concept: 'the curtain wall', title: 'Castle', parent_id: 'n2' },
    { id: 'n5', kind: 'concept', concept: 'arrow slit', title: 'Arrow slit', parent_id: null },
    { id: 'n6', kind: 'concept', concept: 'x', title: '   ', parent_id: null },
    { id: 'n5', kind: 'concept', concept: 'arrow slit', title: 'Arrow slit', parent_id: null },
  ],
  edges: [],
}

describe('pickablePages', () => {
  it('lists each titled node once, sorted by title case-insensitively', () => {
    const out = pickablePages(snap)
    expect(out.map(p => p.id)).toEqual(['n5', 'n1', 'n2', 'n4', 'n3'])
    expect(out[0]).toEqual({ id: 'n5', title: 'Arrow slit' })
  })
  it('a shared title carries its concept when that says more', () => {
    const out = pickablePages(snap)
    expect(out.find(p => p.id === 'n4')).toEqual({ id: 'n4', title: 'Castle', note: 'the curtain wall' })
    expect(out.find(p => p.id === 'n2')).toEqual({ id: 'n2', title: 'Castle' })
  })
  it('filters by a title query, case-insensitively', () => {
    expect(pickablePages(snap, 'CAST').map(p => p.id)).toEqual(['n1', 'n2', 'n4'])
    expect(pickablePages(snap, '  slit ').map(p => p.id)).toEqual(['n5'])
    expect(pickablePages(snap, 'zzz')).toEqual([])
  })
  it('reads the /v1/books/pages answer the same way', () => {
    expect(pickablePages({ pages: snap.nodes }).map(p => p.id)).toEqual(['n5', 'n1', 'n2', 'n4', 'n3'])
    expect(pickablePages({ pages: snap.nodes }, 'wall')).toEqual([])
    expect(pickablePages({ pages: [] })).toEqual([])
  })
  it('survives an empty or odd snapshot', () => {
    expect(pickablePages(null)).toEqual([])
    expect(pickablePages({})).toEqual([])
    expect(pickablePages({ nodes: [null, { id: 3, title: 'x' }, { id: 'a' }] })).toEqual([])
  })
})

describe('defaultTitle', () => {
  it('matches the server: My wanderings, the page title, Since <local date>', () => {
    expect(defaultTitle({ kind: 'all' })).toBe('My wanderings')
    expect(defaultTitle({ kind: 'branch', node_id: 'n1' }, 'Castle')).toBe('Castle')
    expect(defaultTitle({ kind: 'since', since: 0 }, '', new Date(2026, 10, 9))).toBe('Since 9 November 2026')
    const since = new Date(2026, 0, 31, 0, 0, 0).getTime() / 1000
    expect(defaultTitle({ kind: 'since', since })).toBe('Since 31 January 2026')
  })
  it('is cut to 120 characters, as the server cuts it', () => {
    expect(defaultTitle({ kind: 'branch', node_id: 'n' }, 'x'.repeat(200))).toHaveLength(120)
  })
})

describe('formatSize', () => {
  it('gives one decimal under 10 and whole numbers above', () => {
    expect(formatSize(3.2 * 1024 * 1024)).toBe('3.2 MB')
    expect(formatSize(81 * 1024 * 1024 + 1000)).toBe('81 MB')
    expect(formatSize(1536)).toBe('1.5 KB')
    expect(formatSize(500)).toBe('500 bytes')
    expect(formatSize(2.5 * 1024 ** 3)).toBe('2.5 GB')
    expect(formatSize(9.97 * 1024 * 1024)).toBe('10 MB')
  })
  it('is empty for nonsense', () => {
    expect(formatSize(-1)).toBe('')
    expect(formatSize(undefined)).toBe('')
    expect(formatSize('x')).toBe('')
  })
})

describe('sinceSeconds', () => {
  it('reads a date input as local midnight', () => {
    expect(sinceSeconds('2026-11-09')).toBe(new Date(2026, 10, 9).getTime() / 1000)
  })
  it('refuses a bad or out-of-range date', () => {
    for (const v of ['', '2026-13-01', '2026-02-30', '09/11/2026', null, '2100-06-01']) expect(sinceSeconds(v)).toBeNull()
  })
})

describe('scopeBody', () => {
  const include = allIncluded()
  it('builds the three scopes', () => {
    expect(scopeBody({ kind: 'all', include, title: '', by: '' }))
      .toEqual({ scope: { kind: 'all' }, include, title: '', by: '' })
    expect(scopeBody({ kind: 'branch', nodeId: 'n1', include, title: ' Castles ', by: ' Nell ' }))
      .toEqual({ scope: { kind: 'branch', node_id: 'n1' }, include, title: 'Castles', by: 'Nell' })
    expect(scopeBody({ kind: 'since', since: '2026-11-09', include }).scope)
      .toEqual({ kind: 'since', since: new Date(2026, 10, 9).getTime() / 1000 })
  })
  it('is null while the scope is incomplete', () => {
    expect(scopeBody({ kind: 'branch', nodeId: '', include })).toBeNull()
    expect(scopeBody({ kind: 'since', since: '', include })).toBeNull()
    expect(scopeBody({ kind: 'other', include })).toBeNull()
    expect(scopeBody(null)).toBeNull()
  })
  it('sends every include switch, and cuts title and By to their limits', () => {
    const b = scopeBody({ kind: 'all', include: { pictures: false }, title: 'x'.repeat(300), by: 'y'.repeat(300) })
    expect(b.include).toEqual({ pictures: false, maps: true, timelines: true, diagrams: true, stories: true })
    expect(b.title).toHaveLength(120)
    expect(b.by).toHaveLength(80)
  })
})

describe('the words', () => {
  it('previewText', () => {
    expect(previewText({ chapters: 40, pictures: 52, bytes: 81 * 1024 * 1024 })).toBe('40 chapters, 52 pictures, about 81 MB')
    expect(previewText({ chapters: 1, pictures: 1, bytes: 2048 })).toBe('1 chapter, 1 picture, about 2.0 KB')
    expect(previewText({ chapters: 0, pictures: 0, bytes: 0 })).toMatch(/^Nothing to put in the book/)
    expect(previewText(null)).toBe('')
  })
  it('progressText', () => {
    expect(progressText({ status: 'making', progress: { done: 12, total: 40, stage: 'epub' } })).toBe('EPUB: chapter 12 of 40')
    expect(progressText({ status: 'making', progress: { done: 3, total: 40, stage: 'html' } })).toBe('HTML: chapter 3 of 40')
    expect(progressText({ status: 'making', progress: null })).toBe('Starting…')
    expect(progressText({ status: 'queued' })).toBe('Waiting to start…')
    expect(progressText({ status: 'done' })).toBe('Done')
    expect(progressText({ status: 'failed', error: 'Primer shut down' })).toBe('Failed: Primer shut down')
    expect(progressText(null)).toBe('')
  })
  it('isActive, sizesText and bookDate', () => {
    expect(isActive({ status: 'queued' })).toBe(true)
    expect(isActive({ status: 'making' })).toBe(true)
    expect(isActive({ status: 'done' })).toBe(false)
    expect(sizesText({ epub_size: 3.2 * 1024 * 1024, html_size: 4.1 * 1024 * 1024 })).toBe('EPUB 3.2 MB · HTML 4.1 MB')
    expect(sizesText({ epub_size: null, html_size: null })).toBe('')
    expect(bookDate(new Date(2026, 10, 10, 14, 5).getTime() / 1000)).toBe('10 November 2026, 14:05')
    expect(bookDate(null)).toBe('')
  })
  it('bookErrorText keeps the server reason', () => {
    expect(bookErrorText({ status: 413, message: 'Too big: leave out pictures or choose less' }))
      .toBe('Too big: leave out pictures or choose less')
    expect(bookErrorText({ status: 404, message: 'that page was never read' })).toBe('That page was never read, so it has no chapter.')
    expect(bookErrorText({ status: 404, message: 'unknown page' })).toBe('That page is not in the trail any more.')
    expect(bookErrorText(new Error(''))).toBe('Could not reach Primer.')
    expect(bookErrorText(new TypeError('Failed to fetch'))).toBe('Could not reach Primer.')
    expect(bookErrorText({ status: 500, message: '' })).toBe('Primer answered 500.')
  })
})
