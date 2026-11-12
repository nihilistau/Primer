/* the desktop's Kairos note and the Engines switch render (server-side render: no DOM) */
import { describe, expect, test } from 'vitest'
import { renderToString } from 'react-dom/server'
import KairosNote from './KairosNote.jsx'
import { KairosSwitch } from './apps/Engines.jsx'
import { kairosNote } from './remote.js'

const flat = (el) => renderToString(el).replace(/<!-- -->/g, '').replace(/<[^>]+>/g, ' ').replace(/\s+/g, ' ').trim()

describe('Kairos in the window', () => {
  test('the note says what Kairos asked, inside a live region that is always there', () => {
    const note = { text: 'show me Bodiam Castle', key: 1 }
    expect(flat(<KairosNote note={note} />)).toBe('Kairos asked: show me Bodiam Castle')
    expect(renderToString(<KairosNote note={note} />))
      .toMatch(/^<div class="kairos-live" role="status" aria-live="polite"><span class="kairos-note">/)
    // no note: the same empty region, so a screen reader hears the next one
    expect(renderToString(<KairosNote note={null} />)).toBe('<div class="kairos-live" role="status" aria-live="polite"></div>')
  })
  test("the note's text comes from the command, as the server resolved it", () => {
    const show = kairosNote({ id: 'c1', kind: 'show', args: { text: 'tell me a story about Trebuchet' } }, 7)
    expect(show).toEqual({ text: 'tell me a story about Trebuchet', key: 7 })
    expect(flat(<KairosNote note={show} />)).toBe('Kairos asked: tell me a story about Trebuchet')
    const lens = kairosNote({ id: 'c2', kind: 'lens', args: { kind: 'map', id: 'n1', title: 'Trebuchet' } })
    expect(flat(<KairosNote note={lens} />)).toBe('Kairos asked: a map of Trebuchet')
    expect(typeof lens.key).toBe('number')
  })
  test('the switch beside Thinking ahead, with its hint, follows the status', () => {
    const on = renderToString(<KairosSwitch st={{ remote: { enabled: true, listening: true } }} busy={false} onChange={() => {}} />)
    expect(flat(<KairosSwitch st={{}} busy={false} onChange={() => {}} />))
      .toBe('Let Kairos drive Primer Kairos can open pages and lenses here when you ask it to.')
    expect(on).toContain('role="switch"')
    // named by its label: aria-labelledby points at the span that says it
    const labelledby = on.match(/aria-labelledby="([^"]+)"/)[1]
    expect(on).toContain(`id="${labelledby}">Let Kairos drive Primer</span>`)
    const describedby = on.match(/aria-describedby="([^"]+)"/)[1]
    expect(on).toContain(`id="${describedby}">Kairos can open pages`)
    expect(on).toMatch(/checked=""/)
    expect(renderToString(<KairosSwitch st={{ remote: { enabled: false } }} busy onChange={() => {}} />)).not.toMatch(/checked=""/)
  })
})
