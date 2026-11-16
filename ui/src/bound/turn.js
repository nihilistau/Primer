/* turn — the bound book's page turning, pure and DOM-free.
 *
 * The state is { at, turning: {dir, progress, target, mode} | null }:
 *   - at: the open spread's index;
 *   - turning: the leaf in flight, dir +1 (forward) or -1 (back), progress 0..1 (0 = where it
 *     lay, 1 = turned over). target is null while the reader drags it, then 1 (completing) or
 *     0 (falling back); mode is 'drag' or 'step' (a click or key: a timed turn, eased).
 *
 * One leaf per action: start, step and jump first finish a turn in flight (as it was heading),
 * so rapid clicks or keys each turn exactly one leaf and never skip or merge.
 *
 * The curl: the leaf is drawn as STRIPS vertical strips, hinged one to the next, each rotated a
 * little more than the one before, so the free edge leads; `curl` gives their angles. */

export const STRIPS = 6
export const TURN_MS = 600
const MAX_BEND = 14 // degrees between the first and the last strip's extra rotation, at mid-turn

const clamp01 = (p) => (Number.isFinite(p) ? Math.min(1, Math.max(0, p)) : 0)
const inRange = (k, count) => Number.isInteger(k) && k >= 0 && k < count

export const idle = (at = 0) => ({ at: Math.max(0, at | 0), turning: null })

/* where a turn in flight ends: completed (at + dir) or fallen back (at) */
export function finish(s) {
  if (!s || !s.turning) return s
  const t = s.turning
  const completes = t.target === null || t.target === undefined ? t.progress > 0.5 : t.target === 1
  return { at: completes ? s.at + t.dir : s.at, turning: null }
}

/* lift the leaf toward spread at + dir (a drag begins); nothing happens past either cover */
export function start(s, dir, count) {
  const f = finish(s)
  const d = dir < 0 ? -1 : 1
  if (!inRange(f.at + d, count)) return f
  return { at: f.at, turning: { dir: d, progress: 0, target: null, mode: 'drag' } }
}

/* the dragged leaf follows the pointer */
export function drag(s, progress) {
  if (!s || !s.turning || s.turning.target !== null) return s
  return { ...s, turning: { ...s.turning, progress: clamp01(progress) } }
}

/* let go: past the middle the turn completes, otherwise it falls back */
export function release(s) {
  if (!s || !s.turning || s.turning.target !== null) return s
  return { ...s, turning: { ...s.turning, target: s.turning.progress > 0.5 ? 1 : 0 } }
}

/* a click or key: one leaf, timed */
export function step(s, dir, count) {
  const n = start(s, dir, count)
  if (!n.turning) return n
  return { ...n, turning: { ...n.turning, target: 1, mode: 'step' } }
}

/* straight to spread `to` (Home, End, a link, the go-to list): no leaf */
export function jump(s, to, count) {
  const f = finish(s) || idle()
  if (!(count > 0)) return f
  return { at: Math.min(count - 1, Math.max(0, to | 0)), turning: null }
}

/* advance the animation by dt ms. A step runs at an even pace (eased when drawn); a released
 * leaf springs: fast at first, slowing as it settles. Reaching its target finishes the turn. */
export function tick(s, dt, ms = TURN_MS) {
  if (!s || !s.turning || s.turning.target === null) return s
  const t = s.turning
  const d = t.target - t.progress
  const pace = Math.max(0, dt) / ms
  let p
  if (t.mode === 'step') p = t.progress + Math.sign(d) * Math.min(Math.abs(d), pace)
  else p = t.progress + Math.sign(d) * Math.min(Math.abs(d), Math.max(pace * 0.5, Math.abs(d) * Math.min(1, dt / 90)))
  if (Math.abs(t.target - p) < 0.002) return finish(s)
  return { ...s, turning: { ...t, progress: p } }
}

const easeInOut = (p) => (p < 0.5 ? 4 * p * p * p : 1 - Math.pow(-2 * p + 2, 3) / 2)

/* the progress as drawn: a step eases in and out, a drag follows the pointer as it is */
export const shown = (t) => (!t ? 0 : t.mode === 'step' ? easeInOut(clamp01(t.progress)) : clamp01(t.progress))

/* The leaf's angles at progress p. The leaf is hinged at the spine: 0deg lies on the right,
 * -180deg on the left. Going forward it starts on the right; going back, on the left.
 * Returns {base, deltas, angles}: base is the first strip's rotation, deltas each strip's
 * rotation relative to the one before (deltas[0] = 0; they grow toward the free edge, which
 * leads), and angles each strip's absolute rotation. All bends vanish at p = 0 and p = 1. */
export function curl(p, dir = 1, n = STRIPS) {
  const q = clamp01(p)
  const base = dir < 0 ? -180 * (1 - q) : -180 * q
  const bend = MAX_BEND * Math.sin(Math.PI * q)
  const sign = dir < 0 ? 1 : -1
  const deltas = Array.from({ length: n }, (_, i) => (n > 1 ? (sign * bend * i) / (n - 1) : 0))
  const angles = []
  let a = base
  deltas.forEach((d) => { a += d; angles.push(Math.max(-180, Math.min(0, a))) })
  return { base, deltas, angles }
}

/* how dark a strip of the leaf looks at absolute angle a (deg): lit flat, darkest edge-on */
export const shade = (a) => 0.42 * (1 - Math.abs(Math.cos((a * Math.PI) / 180)))

/* The progress for a pointer at x while dragging: the leaf's free edge follows the pointer.
 * spineX is the spine's x and w a page's width, in the same units as x. */
export function dragProgress(x, spineX, w, dir) {
  if (!(w > 0)) return 0
  const r = Math.min(1, Math.max(-1, dir < 0 ? (spineX - x) / w : (x - spineX) / w))
  return Math.acos(r) / Math.PI
}
