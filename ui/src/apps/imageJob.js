/* imageJob — what an image job's status means to the Image window, and polling one to its end. */
import { imageJob } from '../api.js'
import { createPoller } from './poller.js'

const ENDED = new Set(['done', 'failed', 'cancelled'])

/* {label, pct, done, failed, canRetry} for a media dict from GET /v1/images/<id>. `done` means
 * the job has ended (done, failed or cancelled). pct is 0-100 and only moves while generating;
 * once every step is sampled the label is "Decoding…" with the bar full (not during the
 * high-noise stage of a two-stage video model, which is labelled "High-noise step N of M"). */
export function describeJob(job) {
  const s = job?.status
  const out = { label: '', pct: 0, done: ENDED.has(s), failed: s === 'failed', canRetry: s === 'failed' }
  if (s === 'prompting') out.label = 'Writing the prompt…'
  else if (s === 'queued') out.label = 'Waiting for the GPU'
  else if (s === 'loading') out.label = 'Loading the model…'
  else if (s === 'generating') {
    const { step = 0, total = 0, stage } = job.progress || {}
    // a two-stage video model's high-noise stage ends at its own total, which is not the end
    if (total && step >= total && stage !== 'high') { out.label = 'Decoding…'; out.pct = 100 } // sampled; the VAE decodes
    else {
      out.label = total ? `${stage === 'high' ? 'High-noise step' : 'Step'} ${step} of ${total}` : 'Generating…'
      out.pct = total ? Math.max(0, Math.min(100, Math.round(step / total * 100))) : 0
    }
  } else if (s === 'done') { out.label = 'Done'; out.pct = 100 }
  else if (s === 'failed') out.label = `Failed: ${job.error || 'unknown error'}`
  else if (s === 'cancelled') out.label = 'Cancelled'
  else out.label = s || ''
  return out
}

/* sd-server's live preview frame as a data URL, or '' unless it is clean base64. */
export const previewUrl = (b64) =>
  typeof b64 === 'string' && /^[A-Za-z0-9+/]+={0,2}$/.test(b64) ? `data:image/png;base64,${b64}` : ''

/* "1024×768 · 20 steps · cfg 7 · euler" from a settings dict. */
export function settingsSummary(st = {}) {
  const parts = []
  if (st.width && st.height) parts.push(`${st.width}×${st.height}`)
  if (st.steps) parts.push(`${st.steps} steps`)
  if (st.cfg !== undefined && st.cfg !== null) parts.push(`cfg ${st.cfg}`)
  if (st.sampler) parts.push(st.sampler)
  return parts.join(' · ')
}

/* Poll job `id` every `interval` ms, calling onUpdate(job) with each reading. Resolves with
 * the final job once it is done, failed or cancelled; resolves undefined if `signal` aborts
 * first. A failed poll goes to onError and polling carries on, except a 404 (the job no longer
 * exists), which also ends polling. `fetchJob` is a test seam. */
export function pollJob(id, onUpdate, { interval = 1000, signal, onError = () => {}, fetchJob = imageJob } = {}) {
  return new Promise((resolve) => {
    let timer = null
    let poller = null
    const finish = (job) => { clearInterval(timer); poller.stop(); signal?.removeEventListener('abort', onAbort); resolve(job) }
    const onAbort = () => finish(undefined)
    poller = createPoller(() => fetchJob(id), (job) => {
      onUpdate(job)
      if (ENDED.has(job.status)) finish(job)
    }, (e) => {
      onError(e)
      if (e && e.status === 404) finish(undefined)   // the job is gone: stop asking
    })
    if (signal?.aborted) return finish(undefined)
    signal?.addEventListener('abort', onAbort)
    poller.tick()
    timer = setInterval(() => poller.tick(), interval)
  })
}
