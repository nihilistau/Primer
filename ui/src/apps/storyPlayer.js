/* storyPlayer — the Story window's playback as a pure state machine. The window feeds it what
 * happens (play, pause, an audio element's end, scenes becoming ready, a seek) and plays what
 * the state says: scene `scene`, audio running when `playing`. A scene that is wanted but not
 * ready yet puts the player in `waiting` (the window says "Waiting for scene N…"); it carries
 * on by itself the moment that scene is ready. */

/* ready: one boolean per scene, true once its narration exists */
export const initial = (ready = []) => ({ scene: 0, playing: false, waiting: false, finished: false, ready })

/* a scene can play once it is done (a failed or unfinished one cannot) */
export const readyList = (scenes) => (scenes || []).map(s => !!s && s.status === 'done')

/* the 1-based number of the scene being waited for */
export const waitingFor = (state) => state.scene + 1

// the player wants scene `scene` to run: it does if it is ready, else it waits
const want = (s) => (s.ready[s.scene] ? { ...s, playing: true, waiting: false } : { ...s, playing: false, waiting: true })

export function next(state, event) {
  const s = state
  switch (event.type) {
    case 'play':
      if (s.playing) return s
      return want(s.finished ? { ...s, scene: 0, finished: false } : s)
    case 'pause':
      return { ...s, playing: false, waiting: false }
    case 'ready': {
      const t = { ...s, ready: event.ready || [] }
      return t.waiting && t.ready[t.scene] ? { ...t, playing: true, waiting: false } : t
    }
    case 'ended': {
      if (!s.playing) return s            // a late event from an audio element that was already stopped
      if (s.scene + 1 >= s.ready.length) return { ...s, playing: false, waiting: false, finished: true }
      return want({ ...s, scene: s.scene + 1 })
    }
    case 'seek': {
      const last = Math.max(s.ready.length - 1, 0)
      const scene = Math.min(Math.max(Number.isFinite(event.scene) ? Math.trunc(event.scene) : 0, 0), last)
      const t = { ...s, scene, finished: false }
      return s.playing || s.waiting ? want(t) : { ...t, playing: false, waiting: false }
    }
    case 'reset':
      return initial([])
    default:
      return s
  }
}

/* Which chunk of a scene is being spoken at `seconds` into its audio: the latest whose start
 * (t0) is at or before that time, so the sentence just spoken stays lit through the gap after
 * it. chunks are [[start, end, t0, t1], ...]; -1 when there are none. */
export function chunkAt(chunks, seconds) {
  if (!chunks || !chunks.length) return -1
  let at = 0
  for (let i = 0; i < chunks.length; i++) if (chunks[i][2] <= seconds) at = i
  return at
}
