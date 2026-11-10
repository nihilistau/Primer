/* the Book window renders its parts (server-side render: no DOM, no effects) */
import { describe, expect, test } from 'vitest'
import { renderToString } from 'react-dom/server'
import BookWindow, { BookRow, Downloads } from './BookWindow.jsx'

const flat = (el) => renderToString(el).replace(/<[^>]+>/g, ' ').replace(/&#x27;/g, "'").replace(/\s+/g, ' ')
const noop = () => {}
const done = { id: '0123456789abcdef', title: 'Castles', status: 'done', created_at: new Date(2026, 10, 10, 9, 30).getTime() / 1000,
               epub_size: 3.2 * 1024 * 1024, html_size: 4.1 * 1024 * 1024, error: null, progress: null }

describe('Book window', () => {
  test('opens on Everything, with the fields, the switches and Make the book', () => {
    const t = flat(<BookWindow />)
    for (const s of ['Everything I’ve read', 'A page and what branched from it', 'Since a date', 'Title', 'By (optional)',
                     'Pictures', 'Maps', 'Timelines', 'Diagrams', 'Stories', 'Make the book', 'Earlier books']) {
      expect(t).toContain(s)
    }
    const html = renderToString(<BookWindow />)
    expect(html).toContain('placeholder="My wanderings"')
    expect(html).toContain('maxLength="120"')
    expect(html).toContain('maxLength="80"')
    expect(html).toMatch(/<fieldset[^>]*><legend[^>]*>What goes in<\/legend>/)
  })
  test('a finished book downloads as EPUB and HTML', () => {
    const html = renderToString(<Downloads book={done} />)
    expect(html).toContain('href="/v1/books/0123456789abcdef/epub" download=""')
    expect(html).toContain('href="/v1/books/0123456789abcdef/html" download=""')
    expect(renderToString(<Downloads book={{ ...done, status: 'making' }} />)).toBe('')
  })
  test('an earlier book shows its title, date, sizes and Delete; confirming asks inside the window', () => {
    const t = flat(<BookRow book={done} onAsk={noop} onCancel={noop} onDelete={noop} />)
    expect(t).toContain('Castles')
    expect(t).toContain('10 November 2026, 09:30')
    expect(t).toContain('EPUB 3.2 MB · HTML 4.1 MB')
    expect(t).toContain('Delete')
    expect(t).not.toContain('Delete this book?')
    const h = renderToString(<BookRow book={done} onAsk={noop} onCancel={noop} onDelete={noop} />)
    expect(h).toContain('aria-label="Delete “Castles”"')
    expect(h).toContain('aria-label="Download the EPUB of “Castles”"')
    expect(h).toContain('aria-label="Download the HTML of “Castles”"')
    const c = flat(<BookRow book={done} confirming onAsk={noop} onCancel={noop} onDelete={noop} />)
    expect(c).toContain('Delete this book?')
    expect(c).toContain('Keep')
  })
  test('a book being made shows its progress, a failed one its error', () => {
    const making = { ...done, status: 'making', progress: { done: 12, total: 40, stage: 'epub' }, epub_size: null, html_size: null }
    expect(flat(<BookRow book={making} onAsk={noop} onCancel={noop} onDelete={noop} />)).toContain('EPUB: chapter 12 of 40')
    const failed = { ...making, status: 'failed', error: 'The book couldn’t be made' }
    expect(flat(<BookRow book={failed} onAsk={noop} onCancel={noop} onDelete={noop} />)).toContain('Failed: The book couldn’t be made')
  })
})
