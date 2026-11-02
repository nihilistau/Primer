import { describe, expect, it } from 'vitest'
import { againBody, animateButton, describeVideo, lengthText, videoButton, videoCheckFailed, videoChecking,
         videoLength, videoModelChoices } from './videoJob.js'

describe('describeVideo', () => {
  const d = (status, extra = {}) => describeVideo({ status, ...extra })

  it('labels every status like the Image window', () => {
    expect(d('prompting').label).toBe('Writing the prompt…')
    expect(d('queued').label).toBe('Waiting for the GPU')
    expect(d('loading').label).toBe('Loading the model…')
    expect(d('generating', { progress: { step: 3, total: 12 } })).toMatchObject({ label: 'Step 3 of 12', pct: 25 })
    expect(d('done')).toMatchObject({ label: 'Done', done: true, failed: false })
    expect(d('failed', { error: 'out of memory' })).toMatchObject({ label: 'Failed: out of memory', canRetry: true })
    expect(d('cancelled')).toMatchObject({ label: 'Cancelled', done: true, canRetry: false })
  })

  it('says Decoding once sampling is finished (the VAE decode of a video takes a while)', () => {
    expect(d('generating', { progress: { step: 12, total: 12 } })).toMatchObject({ label: 'Decoding…', pct: 100, done: false })
  })
})

describe('lengthText', () => {
  it('reads frames, fps and seconds', () => {
    expect(lengthText(49, 24)).toBe('49 frames · 24 fps · 2.0 s')
    expect(lengthText(121, 24)).toBe('121 frames · 24 fps · 5.0 s')
    expect(lengthText(1, 16)).toBe('1 frame · 16 fps · 0.1 s')
  })
  it('leaves out what it does not know', () => {
    expect(lengthText(49, null)).toBe('49 frames')
    expect(lengthText(null, 24)).toBe('')
    expect(lengthText(0, 24)).toBe('')
  })
  it('prefers what was made over what was asked', () => {
    expect(videoLength({ frames: 49, fps: 24, settings: { frames: 50, fps: 16 } })).toBe('49 frames · 24 fps · 2.0 s')
    expect(videoLength({ frames: null, fps: null, settings: { frames: 81, fps: 16 } })).toBe('81 frames · 16 fps · 5.1 s')
    expect(videoLength({})).toBe('')
  })
})

describe('animateButton', () => {
  it('enabled when Animate can run', () =>
    expect(animateButton({ animate_available: { ok: true, reason: '', model_id: 'm' } })).toEqual({ enabled: true, reason: '' }))
  it('disabled with the reason', () =>
    expect(animateButton({ animate_available: { ok: false, reason: 'no video model that can animate a picture has been added', model_id: null } }))
      .toEqual({ enabled: false, reason: 'no video model that can animate a picture has been added' }))
  it('disabled with a default reason when nothing is known', () => {
    expect(animateButton(null).enabled).toBe(false)
    expect(animateButton(null).reason).toBeTruthy()
    expect(animateButton({ animate_available: { ok: false } }).reason).toBeTruthy()
  })
})

describe('videoButton', () => {
  it('enabled when video is available, else the reason', () => {
    expect(videoButton({ video_available: { ok: true, reason: '' } })).toEqual({ enabled: true, reason: '' })
    expect(videoButton({ video_available: { ok: false, reason: 'no video model added yet' } }))
      .toEqual({ enabled: false, reason: 'no video model added yet' })
    expect(videoButton(null).reason).toBeTruthy()
  })
  it('checking and failed states', () => {
    expect(videoChecking).toMatchObject({ enabled: false, checking: true })
    expect(videoCheckFailed(new Error('boom')).reason).toBe("Couldn't check video availability: boom")
  })
})

describe('againBody', () => {
  const job = { node_id: 'n1', phrase: 'a mill', model_id: 'wan', prompt: 'a water mill turning',
                settings: { frames: 49, fps: 24, seed: 7, steps: 20, result_fps: 24, result_frames: 49 } }

  it('posts a video again with a new seed', () => {
    expect(againBody(job, 'a mill')).toEqual({
      node_id: 'n1', phrase: 'a mill', subject: 'a mill', kind: 'video', model_id: 'wan',
      prompt: 'a water mill turning', overrides: { frames: 49, fps: 24, seed: -1, steps: 20 } })
  })
  it('carries animate_from from the old settings', () => {
    const b = againBody({ ...job, settings: { ...job.settings, animate_from: 'img1' } }, 'a mill')
    expect(b.animate_from).toBe('img1')
    expect(b.overrides).not.toHaveProperty('animate_from')
  })
  it('extra fields win', () => {
    const b = againBody(job, 'a mill', { prompt: 'new', overrides: { seed: -1, frames: 81 } })
    expect(b.prompt).toBe('new')
    expect(b.overrides).toEqual({ seed: -1, frames: 81 })
  })
})

describe('videoModelChoices', () => {
  const families = { wan5: { kind: 'video', can_animate: true, needs_init: false },
                     i2v: { kind: 'video', can_animate: true, needs_init: true },
                     t2v: { kind: 'video', can_animate: false, needs_init: false },
                     sdxl: { kind: 'image', can_animate: false, needs_init: false } }
  const models = [
    { id: 'a', family: 'wan5', kind: 'video', problems: [] },
    { id: 'b', family: 'i2v', kind: 'video', problems: [] },
    { id: 'c', family: 't2v', kind: 'video', problems: [] },
    { id: 'd', family: 'sdxl', kind: 'image', problems: [] },
    { id: 'e', family: 't2v', kind: 'video', problems: ['file not found'] },
  ]
  it('text-to-video: usable video models that take no picture, not the current one', () =>
    expect(videoModelChoices(models, families, { animating: false, current: 'a' }).map(m => m.id)).toEqual(['c']))
  it('animating: only models that can animate', () =>
    expect(videoModelChoices(models, families, { animating: true, current: 'x' }).map(m => m.id)).toEqual(['a', 'b']))
})
