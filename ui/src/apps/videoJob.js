/* videoJob — what a video job means to the Video window: its status (the same states as an
 * image job), its length, whether the Video and Animate buttons work, and the body that runs
 * it again. Polling is imageJob's: a video is a /v1/images job of kind video. */
import { describeJob } from './imageJob.js'

export const describeVideo = describeJob

/* "49 frames · 24 fps · 2.0 s"; '' without a frame count, no fps or seconds without fps */
export function lengthText(frames, fps) {
  if (!(frames > 0)) return ''
  const f = `${frames} ${frames === 1 ? 'frame' : 'frames'}`
  if (!(fps > 0)) return f
  return `${f} · ${fps} fps · ${(frames / fps).toFixed(1)} s`
}

/* a job's length: what was made once done, else what was asked */
export const videoLength = (job = {}) =>
  lengthText(job.frames || job.settings?.frames, job.fps || job.settings?.fps)

const button = (a, fallback) =>
  a && a.ok ? { enabled: true, reason: '' } : { enabled: false, reason: (a && a.reason) || fallback }

/* {enabled, reason} for a Video button from a /v1/status (or /v1/image-models) reply */
export const videoButton = (st) => button(st && st.video_available, 'Video is not available right now.')

/* {enabled, reason} for Animate from a /v1/status or /v1/image-models reply */
export const animateButton = (st) => button(st && st.animate_available, 'Animate is not available right now.')

export const videoChecking = { enabled: false, checking: true, reason: 'Checking whether video is available…' }
export const videoCheckFailed = (e) =>
  ({ enabled: false, reason: `Couldn't check video availability: ${(e && e.message) || 'Primer did not answer.'}` })

/* settings keys that describe a finished job, not a request */
const NOT_OVERRIDES = new Set(['animate_from', 'result_fps', 'result_frames'])

/* The POST /v1/images body that runs `job` again as a new video with a new seed. The server
 * does not carry animate_from over by itself, so an Animate job sends it back. `extra` wins. */
export function againBody(job, subject = '', extra = {}) {
  const st = job.settings || {}
  const overrides = Object.fromEntries(Object.entries(st).filter(([k]) => !NOT_OVERRIDES.has(k)))
  return {
    node_id: job.node_id, phrase: job.phrase || '', subject, kind: 'video',
    model_id: job.model_id, prompt: job.prompt || undefined,
    overrides: { ...overrides, seed: -1 },
    ...(st.animate_from ? { animate_from: st.animate_from } : {}),
    ...extra,
  }
}

/* Usable video models this job could run on instead of `current`: an Animate job needs a model
 * that can animate; a text job can't use one that only animates a picture. */
export function videoModelChoices(models = [], families = {}, { animating = false, current } = {}) {
  return models.filter((m) => {
    const fam = families[m.family]
    if (m.kind !== 'video' || m.id === current || (m.problems && m.problems.length) || !fam) return false
    return animating ? !!fam.can_animate : !fam.needs_init
  })
}
