/* sound — the page-turn sound: about 250 ms of filtered noise made with WebAudio, no file.
 * Off by default. No AudioContext exists until the reader switches sound on (`enable`, from that
 * click) or, with sound remembered as on, until the first turn plays it (a click, key or drag,
 * so the browser lets it start). */

export const TURN_SOUND_MS = 250

const defaultCtor = () => (typeof window !== 'undefined' ? window.AudioContext || window.webkitAudioContext : undefined)

export function createTurnSound({ enabled = false, ctor = defaultCtor } = {}) {
  let on = !!enabled
  let ctx = null
  const context = () => {
    if (ctx) return ctx
    const C = ctor()
    if (typeof C !== 'function') return null
    try { ctx = new C() } catch { ctx = null }
    return ctx
  }
  return {
    get enabled() { return on },
    get context() { return ctx },
    enable() {
      on = true
      const c = context()
      try { if (c && c.state === 'suspended' && c.resume) c.resume() } catch { /* stays quiet */ }
    },
    disable() { on = false },
    /* one soft paper turn; quiet when off or when WebAudio is missing */
    play() {
      if (!on) return
      const c = context()
      if (!c) return
      try {
        const secs = TURN_SOUND_MS / 1000
        const n = Math.max(1, Math.floor(c.sampleRate * secs))
        const buf = c.createBuffer(1, n, c.sampleRate)
        const data = buf.getChannelData(0)
        // brown-ish noise: a little smoother than white, more like paper than hiss
        let last = 0
        for (let k = 0; k < n; k++) { last = (last + 0.06 * (Math.random() * 2 - 1)) / 1.06; data[k] = last * 3.2 }
        const src = c.createBufferSource()
        src.buffer = buf
        const band = c.createBiquadFilter()
        band.type = 'bandpass'
        band.Q.value = 0.7
        const t = c.currentTime
        band.frequency.setValueAtTime(2600, t)
        band.frequency.exponentialRampToValueAtTime(700, t + secs)
        const gain = c.createGain()
        gain.gain.setValueAtTime(0.0001, t)
        gain.gain.exponentialRampToValueAtTime(0.22, t + 0.03)
        gain.gain.exponentialRampToValueAtTime(0.0001, t + secs)
        src.connect(band); band.connect(gain); gain.connect(c.destination)
        src.start(t)
        src.stop(t + secs)
      } catch { /* a sound is never worth an error */ }
    },
    close() {
      const c = ctx
      ctx = null
      try { if (c && c.close) c.close() } catch { /* already closed */ }
    },
  }
}
