import { describe, expect, test } from 'vitest'
import {
  MAX_PASTE, WINDOW_ID, askParts, b64Bytes, plainText, capPaste, cleanText, createClickCounter, createInputQueue, framePoint, keyAction, liveSize, liveStatusLine,
  liveStepAction, liveable, modifiers, mouseButton, newWindowId, offerLive, textChunks, wheelDelta,
} from './live.js'

const key = (k, extra = {}) => ({ key: k, ctrlKey: false, altKey: false, metaKey: false, shiftKey: false, ...extra })

describe('window ids', () => {
  test('"live-" and a short random lowercase id the server accepts', () => {
    for (let i = 0; i < 50; i++) {
      const id = newWindowId()
      expect(id).toMatch(/^live-[a-z0-9]{10}$/)
      expect(id).toMatch(WINDOW_ID)
    }
    expect(newWindowId(() => 0)).toBe('live-0000000000')
    expect(newWindowId(() => 0.999)).toBe('live-zzzzzzzzzz')
  })
})

describe('framePoint', () => {
  const rect = { left: 100, top: 50, width: 800, height: 600 }
  test('a frame that fills the box maps straight through', () => {
    expect(framePoint(100, 50, rect, 800, 600)).toEqual({ x: 0, y: 0 })
    expect(framePoint(500, 350, rect, 800, 600)).toEqual({ x: 400, y: 300 })
    expect(framePoint(900, 650, rect, 800, 600)).toEqual({ x: 800, y: 600 })
  })
  test('a smaller frame is scaled up, keeping its aspect', () => {
    expect(framePoint(500, 350, rect, 400, 300)).toEqual({ x: 200, y: 150 })
  })
  test('a wide frame has bars above and below; a point on a bar is no point', () => {
    // 1600x600 in 800x600: scale .5, drawn 800x300, from y=200 to 500
    expect(framePoint(500, 100, rect, 1600, 600)).toBe(null)
    expect(framePoint(500, 350, rect, 1600, 600)).toEqual({ x: 800, y: 300 })
    expect(framePoint(100, 200, rect, 1600, 600)).toEqual({ x: 0, y: 0 })
  })
  test('a tall frame has bars at the sides', () => {
    // 300x600 in 800x600: scale 1, drawn 300 wide, from client x 350 to 650
    expect(framePoint(200, 300, rect, 300, 600)).toBe(null)
    expect(framePoint(700, 300, rect, 300, 600)).toBe(null)
    expect(framePoint(350, 300, rect, 300, 600)).toEqual({ x: 0, y: 250 })
    expect(framePoint(500, 300, rect, 300, 600)).toEqual({ x: 150, y: 250 })
  })
  test('clamp pulls an outside point onto the edge (a drag that leaves the picture)', () => {
    expect(framePoint(0, 0, rect, 800, 600, true)).toEqual({ x: 0, y: 0 })
    expect(framePoint(2000, 2000, rect, 800, 600, true)).toEqual({ x: 800, y: 600 })
    expect(framePoint(0, 0, rect, 800, 600)).toBe(null)
  })
  test('no frame size yet, or an empty box: nothing', () => {
    expect(framePoint(10, 10, rect, 0, 0)).toBe(null)
    expect(framePoint(10, 10, { left: 0, top: 0, width: 0, height: 0 }, 800, 600)).toBe(null)
    expect(framePoint(10, 10, null, 800, 600)).toBe(null)
  })
})

describe('input mapping', () => {
  test('modifier bits are the DevTools ones', () => {
    expect(modifiers(key('a'))).toBe(0)
    expect(modifiers(key('a', { altKey: true }))).toBe(1)
    expect(modifiers(key('a', { ctrlKey: true }))).toBe(2)
    expect(modifiers(key('a', { metaKey: true }))).toBe(4)
    expect(modifiers(key('a', { shiftKey: true }))).toBe(8)
    expect(modifiers(key('a', { ctrlKey: true, shiftKey: true }))).toBe(10)
  })
  test('mouse buttons', () => {
    expect([0, 1, 2, 3].map(mouseButton)).toEqual(['left', 'middle', 'right', 'left'])
  })
  test('wheel deltas in pixels, lines and pages converted, and clamped', () => {
    expect(wheelDelta({ deltaMode: 0, deltaX: 0, deltaY: 120 })).toEqual({ dx: 0, dy: 120 })
    expect(wheelDelta({ deltaMode: 1, deltaX: 1, deltaY: 3 })).toEqual({ dx: 16, dy: 48 })
    expect(wheelDelta({ deltaMode: 2, deltaX: 0, deltaY: -1 }, 500)).toEqual({ dx: 0, dy: -500 })
    expect(wheelDelta({ deltaMode: 0, deltaX: 0, deltaY: 1e6 })).toEqual({ dx: 0, dy: 5000 })
  })
})

describe('keyAction', () => {
  test('Esc goes to the page; Shift+Esc leaves the picture and is never sent', () => {
    expect(keyAction(key('Escape'))).toEqual({ do: 'key', event: { kind: 'key', type: 'down', key: 'Escape', modifiers: 0 } })
    expect(keyAction(key('Escape'), 'up')).toEqual({ do: 'key', event: { kind: 'key', type: 'up', key: 'Escape', modifiers: 0 } })
    expect(keyAction(key('Escape', { shiftKey: true }))).toEqual({ do: 'leave' })
    expect(keyAction(key('Escape', { shiftKey: true }), 'up')).toEqual({ do: 'swallow' })
    expect(keyAction(key('Escape', { shiftKey: true, ctrlKey: true })).do).toBe('key')
  })
  test('printable characters are typed text; their keyup is swallowed', () => {
    expect(keyAction(key('a'))).toEqual({ do: 'text', text: 'a' })
    expect(keyAction(key('A', { shiftKey: true }))).toEqual({ do: 'text', text: 'A' })
    expect(keyAction(key(' '))).toEqual({ do: 'text', text: ' ' })
    expect(keyAction(key('é'))).toEqual({ do: 'text', text: 'é' })
    expect(keyAction(key('😀'))).toEqual({ do: 'text', text: '😀' })
    expect(keyAction(key('a'), 'up')).toEqual({ do: 'swallow' })
  })
  test('AltGr (Ctrl+Alt) characters are text, not shortcuts', () => {
    expect(keyAction(key('@', { ctrlKey: true, altKey: true }))).toEqual({ do: 'text', text: '@' })
  })
  test('named keys go as key events, down and up, with modifiers', () => {
    expect(keyAction(key('Enter'))).toEqual({ do: 'key', event: { kind: 'key', type: 'down', key: 'Enter', modifiers: 0 } })
    expect(keyAction(key('Tab', { shiftKey: true }), 'up')).toEqual({ do: 'key', event: { kind: 'key', type: 'up', key: 'Tab', modifiers: 8 } })
    for (const k of ['Backspace', 'Delete', 'ArrowLeft', 'ArrowDown', 'Home', 'End', 'PageUp', 'PageDown']) {
      expect(keyAction(key(k)).do).toBe('key')
    }
  })
  test('shortcuts go as key events; ctrl/cmd+V is a paste', () => {
    expect(keyAction(key('a', { ctrlKey: true }))).toEqual({ do: 'key', event: { kind: 'key', type: 'down', key: 'a', modifiers: 2 } })
    expect(keyAction(key('v', { ctrlKey: true }))).toEqual({ do: 'paste' })
    expect(keyAction(key('v', { metaKey: true }))).toEqual({ do: 'paste' })
    expect(keyAction(key('v', { ctrlKey: true }), 'up')).toEqual({ do: 'swallow' })
  })
  test('keys that are not for the page are left alone', () => {
    for (const k of ['F5', 'F12', 'Shift', 'Control', 'Alt', 'Meta', 'Unidentified', 'Dead']) expect(keyAction(key(k))).toBe(null)
    expect(keyAction({ ...key('a'), isComposing: true })).toBe(null)
    expect(keyAction({})).toBe(null)
  })
})

describe('text', () => {
  test('clean: CRLF to LF, no NUL or other control characters but tab and newline', () => {
    expect(cleanText('a\r\nb\rc\u0000d\u0007e\tf\ng')).toBe('a\nb\ncde\tf\ng')
  })
  test('cut into posts of at most 1000 characters, never splitting a surrogate pair', () => {
    const t = 'x'.repeat(2500)
    expect(textChunks(t).map(c => c.length)).toEqual([1000, 1000, 500])
    const e = '😀'.repeat(600)  // 1200 UTF-16 units
    const parts = textChunks(e)
    expect(parts.join('')).toBe(e)
    for (const p of parts) {
      expect(p.length).toBeLessThanOrEqual(1000)
      expect(/^[\uDC00-\uDFFF]/.test(p)).toBe(false)
    }
  })
  test('each post stays under the 4 KB input limit, even for wide characters', () => {
    for (const t of ['語'.repeat(3000), '"\\'.repeat(2000), '😀'.repeat(3000)]) {
      for (const p of textChunks(t)) {
        expect(new TextEncoder().encode(JSON.stringify({ kind: 'text', text: p })).length).toBeLessThan(4096)
      }
    }
    expect(textChunks('')).toEqual([])
  })
})

describe('the input queue', () => {
  const setup = () => {
    const sent = []
    const q = createInputQueue(async (ev) => { sent.push(ev) })
    return { q, sent }
  }
  const move = (x) => ({ kind: 'mouse', type: 'move', x, y: 1, button: 'none' })

  test('one move per flush: moves waiting at the end are replaced by the newest', async () => {
    const { q, sent } = setup()
    q.push(move(1)); q.push(move(2)); q.push(move(3))
    expect(q.pending()).toBe(1)
    await q.flush()
    expect(sent).toEqual([move(3)])
  })
  test('order is kept: a move before a click is not merged past it', async () => {
    const { q, sent } = setup()
    const dn = { kind: 'mouse', type: 'down', x: 2, y: 1, button: 'left', clicks: 1 }
    q.push(move(1)); q.push(dn); q.push(move(5)); q.push(move(6))
    await q.flush()
    expect(sent).toEqual([move(1), dn, move(6)])
  })
  test('typed text is joined into one post, up to 1000 characters, around key events in order', async () => {
    const { q, sent } = setup()
    for (const c of 'hello') q.push({ kind: 'text', text: c })
    q.push({ kind: 'key', type: 'down', key: 'Enter', modifiers: 0 })
    q.push({ kind: 'text', text: 'x' })
    await q.flush()
    expect(sent).toEqual([
      { kind: 'text', text: 'hello' },
      { kind: 'key', type: 'down', key: 'Enter', modifiers: 0 },
      { kind: 'text', text: 'x' },
    ])
    q.push({ kind: 'text', text: 'a'.repeat(999) }); q.push({ kind: 'text', text: 'bc' })
    await q.flush()
    expect(sent.slice(3).map(e => e.text.length)).toEqual([1000, 1])
  })
  test('wheel deltas waiting together are added', async () => {
    const { q, sent } = setup()
    q.push({ kind: 'wheel', x: 1, y: 1, dx: 0, dy: 100, modifiers: 0 })
    q.push({ kind: 'wheel', x: 2, y: 2, dx: 0, dy: 60, modifiers: 0 })
    await q.flush()
    expect(sent).toEqual([{ kind: 'wheel', x: 2, y: 2, dx: 0, dy: 160, modifiers: 0 }])
  })
  test('one post at a time: a flush while sending waits for the next frame', async () => {
    let release
    const sent = []
    const q = createInputQueue((ev) => { sent.push(ev); return new Promise(r => { release = r }) })
    q.push(move(1))
    const f = q.flush()
    q.push(move(2)); q.push(move(3))
    await q.flush()                 // busy: nothing more sent
    expect(sent).toEqual([move(1)])
    release(); await f
    const g = q.flush()
    release(); await g
    expect(sent).toEqual([move(1), move(3)])
  })
  test('a failed post is reported and the rest still go', async () => {
    const errs = []
    const sent = []
    const q = createInputQueue(async (ev) => { if (ev.key === 'Tab') throw new Error('no'); sent.push(ev) }, (e) => errs.push(e.message))
    q.push({ kind: 'key', type: 'down', key: 'Tab', modifiers: 0 }); q.push({ kind: 'text', text: 'a' })
    await q.flush()
    expect(errs).toEqual(['no'])
    expect(sent).toEqual([{ kind: 'text', text: 'a' }])
  })
  test('clear drops what waits', async () => {
    const { q, sent } = setup()
    q.push(move(1)); q.clear(); await q.flush()
    expect(sent).toEqual([])
  })
})

describe('Open it live', () => {
  test('a nearly empty page is offered', () => {
    expect(offerLive('')).toBe(true)
    expect(offerLive('# Loading\n\n[Home](https://a.test/)')).toBe(true)
    expect(offerLive('x'.repeat(199))).toBe(true)
    expect(offerLive('x'.repeat(200))).toBe(false)
  })
  test('markup and link addresses do not count as text', () => {
    expect(offerLive(`[${'a'.repeat(50)}](https://example.org/${'p'.repeat(300)})`)).toBe(true)
  })
  test('a page that says it needs scripts is offered, however long', () => {
    const long = 'Words about the museum. '.repeat(40)
    expect(offerLive(long)).toBe(false)
    for (const s of ['Please enable JavaScript to continue', 'This site requires JavaScript', 'JavaScript is disabled in your browser', 'javascript is required']) {
      expect(offerLive(long + s)).toBe(true)
    }
  })
})

describe('the ask', () => {
  test('press, type and choose, with label and value apart for bold', () => {
    expect(askParts({ action: 'press', label: 'Add to basket', value: '' })).toEqual(['The model wants to press ', { b: 'Add to basket' }])
    expect(askParts({ action: 'type', label: 'Search', value: 'Bodiam' }))
      .toEqual(['The model wants to type ', { b: '‘Bodiam’' }, ' into ', { b: 'Search' }])
    expect(askParts({ action: 'choose', label: 'Day', value: 'Saturday' }))
      .toEqual(['The model wants to choose ', { b: 'Saturday' }, ' in ', { b: 'Day' }])
  })
  test('the action as an object, as the step carries it', () => {
    expect(askParts({ action: { action: 'press', n: 3, label: 'Go' }, label: 'Go' })[1]).toEqual({ b: 'Go' })
  })
  test('anything else falls back to the server\'s plain text', () => {
    expect(askParts({ text: 'The model wants to press X', action: 'odd', label: 'X' })).toEqual(['The model wants to press X'])
    expect(askParts(null)).toEqual(['The model wants to act on this page'])
  })
  test('bidi overrides, isolates and zero-width marks are taken out of the label and value', () => {
    expect(askParts({ action: 'press', label: 'Buy \u202estekcit\u202c\u200b' }))
      .toEqual(['The model wants to press ', { b: 'Buy stekcit' }])
    expect(askParts({ action: 'type', label: '\u2066Search\u2069', value: 'moc.elpmaxe\u200d@me\ufeff\u200e' }))
      .toEqual(['The model wants to type ', { b: '‘moc.elpmaxe@me’' }, ' into ', { b: 'Search' }])
    expect(askParts({ text: 'The model wants \u202eto act\x07', action: 'odd' })).toEqual(['The model wants to act'])
  })
  test('plainText', () => {
    expect(plainText(' a\u202ab\u202d c\u2067\u2068d\u200f\u200b\n\t e ')).toEqual('ab cd e')
    expect(plainText(null)).toEqual('')
  })
})

describe('steps, status and sizes', () => {
  test('a step\'s words', () => {
    expect(liveStepAction({ action: { action: 'follow', n: 2, label: 'Opening hours', href: 'https://a.test/h' } })).toBe('follow “Opening hours”')
    expect(liveStepAction({ action: { action: 'scroll', dir: 'up' } })).toBe('scroll up')
    expect(liveStepAction({ action: { action: 'press', n: 7 } })).toBe('press control 7')
    expect(liveStepAction({ action: { action: 'type', n: 1, label: 'Search', text: 'Bodiam' } })).toBe('type ‘Bodiam’ into “Search”')
    expect(liveStepAction({ action: { action: 'choose', n: 4, label: 'Day', option: 'Sat' } })).toBe('choose Sat in “Day”')
    expect(liveStepAction({ action: { action: 'answer' } })).toBe('answer')
    expect(liveStepAction({})).toBe('')
  })
  test('the Setup centre line', () => {
    expect(liveStatusLine({ available: true, label: 'Chrome 158', enabled: true })).toBe('Live pages: Chrome 158')
    expect(liveStatusLine({ available: false, label: '', enabled: true })).toBe('Live pages need Chrome or Edge')
    expect(liveStatusLine({ available: true, label: 'Edge 150', enabled: false })).toBe('Live pages: Edge 150 (off while web search is off)')
    expect(liveStatusLine(undefined)).toBe('')
  })
  test('only web addresses open live', () => {
    expect(liveable('https://a.test/')).toBe(true)
    expect(liveable('http://a.test/')).toBe(true)
    for (const u of ['kiwix:wiki/A', 'file:///c:/x', 'javascript:alert(1)', '', null]) expect(liveable(u)).toBe(false)
  })
  test('sizes are whole numbers from 200 to 4000', () => {
    expect([0, 150.4, 640.6, 9999, NaN].map(liveSize)).toEqual([200, 200, 641, 4000, 200])
  })
  test('base64 to bytes', () => {
    expect(Array.from(b64Bytes('/9j/'))).toEqual([0xff, 0xd8, 0xff])
  })
})

describe('fix round 1', () => {
  test('a double and a triple click are counted locally', () => {
    const c = createClickCounter()
    expect(c.press(10, 10, 'left', 1000)).toBe(1)
    expect(c.press(12, 11, 'left', 1300)).toBe(2)
    expect(c.press(12, 11, 'left', 1600)).toBe(3)
    expect(c.press(12, 11, 'left', 1900)).toBe(3)          // no more than 3
    expect(c.press(12, 11, 'left', 2400)).toBe(1)          // too late
    expect(c.press(30, 11, 'left', 2500)).toBe(1)          // too far
    expect(c.press(30, 11, 'right', 2600)).toBe(1)         // another button
    c.reset()
    expect(c.press(30, 11, 'right', 2700)).toBe(1)
  })
  test('700 é and 1000 中 go in posts the server takes, measured either way', () => {
    for (const t of ['é'.repeat(700), '中'.repeat(1000), 'é"\\'.repeat(900)]) {
      const parts = textChunks(t)
      expect(parts.join('')).toBe(t)
      for (const p of parts) {
        const body = { kind: 'text', text: p }
        expect(new TextEncoder().encode(JSON.stringify(body)).length).toBeLessThan(4096)
        // Python's json.dumps escapes every non-ASCII unit as \uXXXX (6 bytes)
        const escaped = JSON.stringify(body).replace(/[\u0080-\uffff]/g, '      ')
        expect(escaped.length).toBeLessThan(4096)
      }
    }
  })
  test('a paste is cut at 20 000 characters, never inside a surrogate pair', () => {
    expect(capPaste('abc')).toEqual({ text: 'abc', cut: false })
    const big = capPaste('x'.repeat(50000))
    expect(big.cut).toBe(true)
    expect(big.text.length).toBe(MAX_PASTE)
    const emoji = capPaste('a' + '😀'.repeat(20000))
    expect(emoji.text.length).toBe(MAX_PASTE - 1)
    expect(/[\uD800-\uDBFF]$/.test(emoji.text)).toBe(false)
  })
  test('a big paste is chunked quickly (linear)', () => {
    const t0 = Date.now()
    const parts = textChunks('word '.repeat(4000))
    expect(parts.join('')).toBe('word '.repeat(4000))
    expect(Date.now() - t0).toBeLessThan(500)
  })
})
