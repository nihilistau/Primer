import { describe, expect, test } from 'vitest'
import { createTurnSound } from './sound.js'

/* a stand-in AudioContext that counts what is made */
function fakeAudio() {
  const made = { contexts: 0, sources: 0, started: [] }
  const param = () => ({ value: 0, setValueAtTime() {}, exponentialRampToValueAtTime() {} })
  class Ctx {
    constructor() { made.contexts++; this.sampleRate = 8000; this.currentTime = 1; this.state = 'running'; this.destination = {} }
    createBuffer(ch, n) { const d = new Float32Array(n); return { length: n, getChannelData: () => d } }
    createBufferSource() { made.sources++; return { connect() {}, start: (t) => made.started.push(t), stop() {} } }
    createBiquadFilter() { return { type: '', Q: param(), frequency: param(), connect() {} } }
    createGain() { return { gain: param(), connect() {} } }
  }
  return { made, ctor: () => Ctx }
}

describe('the page-turn sound', () => {
  test('off by default: no AudioContext is made, and play is quiet', () => {
    const { made, ctor } = fakeAudio()
    const s = createTurnSound({ ctor })
    expect(s.enabled).toBe(false)
    s.play(); s.play()
    expect(made.contexts).toBe(0)
    expect(s.context).toBe(null)
  })

  test('switching it on makes one context, and each turn plays one sound', () => {
    const { made, ctor } = fakeAudio()
    const s = createTurnSound({ ctor })
    s.enable()
    expect(made.contexts).toBe(1)
    s.play(); s.play()
    expect(made.contexts).toBe(1)
    expect(made.sources).toBe(2)
    s.disable(); s.play()
    expect(made.sources).toBe(2)
  })

  test('remembered as on: the context waits for the first turn', () => {
    const { made, ctor } = fakeAudio()
    const s = createTurnSound({ enabled: true, ctor })
    expect(made.contexts).toBe(0)
    s.play()
    expect(made.contexts).toBe(1)
    expect(made.sources).toBe(1)
  })

  test('no WebAudio at all is quiet, not an error', () => {
    const s = createTurnSound({ enabled: true, ctor: () => undefined })
    expect(() => { s.enable(); s.play(); s.close() }).not.toThrow()
  })
})
