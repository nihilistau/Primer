/* the bound book's shell renders (server-side render: no DOM, no effects) */
import { describe, expect, test } from 'vitest'
import { renderToString } from 'react-dom/server'
import BoundBook, { ASK_BROWSER, OPEN_IN_PRIMER, REBUILT_NOTE } from './BoundBook.jsx'
import { APPS, DOCK } from '../apps.jsx'

describe('the bound book', () => {
  test('opens with the book loading and the bar as a toolbar', () => {
    const html = renderToString(<BoundBook windowId="bound:1" bookId="b1" />)
    expect(html).toContain('Opening the book…')
    expect(html).toMatch(/role="toolbar" aria-label="Book"/)
    for (const s of ['aria-label="Previous page"', 'aria-label="Next page"', 'aria-label="Thumbnails"', '>Night<', '>Day<', '>Sound<']) {
      expect(html).toContain(s)
    }
    expect(html).toContain('aria-live="polite"')
  })
  test('night paper and sound off by default', () => {
    const html = renderToString(<BoundBook windowId="bound:1" scope={{ kind: 'all' }} include={{}} />)
    expect(html).toContain('class="bb bb-night')
    expect(html).toMatch(/aria-pressed="true"[^>]*>Night</)
    expect(html).toMatch(/aria-pressed="false"[^>]*>Sound</)
  })
  test('the copy', () => {
    expect(REBUILT_NOTE).toBe('Rebuilt from your trail as it is now')
    expect(ASK_BROWSER).toBe('Open in the Browser?')
    expect(OPEN_IN_PRIMER).toBe('Open this page in Primer')
  })
  test('a bound window is registered, not in the dock', () => {
    expect(APPS.bound.component).toBe(BoundBook)
    expect(APPS.bound.single).toBeFalsy()
    expect(DOCK).not.toContain('bound')
  })
})

describe('the leaf faces never mirror a page', () => {
  test('no face class is a page kind\'s class (the back cover is kind "back")', async () => {
    const { FACE_FRONT, FACE_BACK } = await import('./BoundBook.jsx')
    const faceClasses = new Set([...FACE_FRONT.split(' '), ...FACE_BACK.split(' ')])
    for (const kind of ['cover', 'back']) expect(faceClasses.has('bb-' + kind)).toBe(false)
  })
  test('only the leaf\'s back face is turned over', async () => {
    const { readFileSync } = await import('node:fs')
    const css = readFileSync(new URL('./bound.css', import.meta.url), 'utf8')
    const turned = [...css.matchAll(/([^{}]+)\{[^}]*rotateY\(180deg\)[^}]*\}/g)].map((m) => m[1].trim())
    expect(turned).toEqual(['.bb-face-back'])
  })
})
