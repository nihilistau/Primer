import { expect, test } from 'vitest'
import { parseSSE } from './sse.js'

test('parseSSE handles split chunks', () => {
  let r = parseSSE('event: node\ndata: {"id":"1"}\n\nevent: del', '')
  expect(r.events).toEqual([{ event: 'node', data: { id: '1' } }])
  r = parseSSE('ta\ndata: {"text":"hi"}\n\n', r.carry)
  expect(r.events).toEqual([{ event: 'delta', data: { text: 'hi' } }])
})
test('parseSSE handles CRLF and several events in one chunk', () => {
  const r = parseSSE('event: a\r\ndata: {"n":1}\r\n\r\nevent: b\r\ndata: {"n":2}\r\n\r\n', '')
  expect(r.events.map(e => e.event)).toEqual(['a', 'b']); expect(r.carry).toBe('')
})
