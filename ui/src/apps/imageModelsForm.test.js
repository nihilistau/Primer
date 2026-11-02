import { describe, expect, it } from 'vitest'
import {
  alignFrames, blankForm, clipSeconds, familyGroups, formFromModel, isDirty, isTwoStage, loraProblems, loraStages, takesLoras,
  modelFromForm, settingFields, settingsFor,
} from './imageModelsForm.js'

const B = String.fromCharCode(92)
const SDXL = {
  label: 'SDXL',
  slots: [{ name: 'model', required: true }, { name: 'vae', required: false }],
  defaults: { width: 1024, height: 1024, steps: 20, cfg: 6, sampler: 'euler_a', vae_tiling: true, offload: false },
}
const MOSS = {
  label: 'MOSS-Image',
  slots: [{ name: 'diffusion', required: true }, { name: 'llm', required: true },
          { name: 'vae', required: true }, { name: 'tokenizer', required: true }],
  defaults: { width: 1024, height: 1024, steps: 20, cfg: 3, sampler: 'euler', flow_shift: 5, offload: true },
}
const model = { id: 'a-1', name: 'A', family: 'sdxl', files: { model: `C:${B}m${B}a.safetensors` }, settings: { steps: 30 } }

describe('settingFields', () => {
  it('lists the schema fields in a fixed order with labels and hints', () => {
    const keys = settingFields(SDXL).map(f => f.key)
    expect(keys).toEqual(['width', 'height', 'steps', 'cfg', 'sampler', 'seed', 'negative', 'offload', 'vae_tiling', 'flash_attn'])
    expect(settingFields(SDXL).every(f => f.label && f.hint)).toBe(true)
  })
  it('shows flow shift only for a family that has it', () => {
    expect(settingFields(MOSS).map(f => f.key)).toContain('flow_shift')
    expect(settingFields(SDXL).map(f => f.key)).not.toContain('flow_shift')
  })
})

describe('form round trip', () => {
  it('fills the family defaults under the model settings and gives back the same model', () => {
    const form = formFromModel(model, SDXL)
    expect(form.settings.steps).toBe('30')
    expect(form.settings.width).toBe('1024')
    const { model: out, errors } = modelFromForm(form, SDXL)
    expect(errors).toEqual([])
    expect(out).toMatchObject({ id: 'a-1', name: 'A', family: 'sdxl', files: { model: `C:${B}m${B}a.safetensors` } })
    expect(out.settings).toMatchObject({ steps: 30, width: 1024, cfg: 6, sampler: 'euler_a', vae_tiling: true, offload: false })
  })
  it('a new model has no id and drops blank file slots and blank optional settings', () => {
    const form = formFromModel({ name: 'N', family: 'moss-image', files: { diffusion: `C:${B}d.gguf`, vae: '' }, settings: {} }, MOSS)
    const { model: out } = modelFromForm({ ...form, files: { ...form.files, llm: `C:${B}l.gguf`, vae: `C:${B}v.gguf`, tokenizer: `C:${B}t.json` } }, MOSS)
    expect(out.id).toBeUndefined()
    expect(out.files).toEqual({ diffusion: `C:${B}d.gguf`, llm: `C:${B}l.gguf`, vae: `C:${B}v.gguf`, tokenizer: `C:${B}t.json` })
    expect(out.settings.flow_shift).toBe(5)
    expect(out.settings).not.toHaveProperty('seed')
    expect(out.settings).not.toHaveProperty('negative')
  })
})

describe('errors', () => {
  const good = () => formFromModel(model, SDXL)
  it('flags numbers out of range or not whole', () => {
    const f = good(); f.settings.steps = '500'; f.settings.width = '100'; f.settings.cfg = 'abc'; f.settings.seed = '1.5'
    const msgs = modelFromForm(f, SDXL).errors.join('|')
    expect(msgs).toMatch(/Steps.*1.*150/)
    expect(msgs).toMatch(/Width.*256.*2048/)
    expect(msgs).toMatch(/CFG.*number/)
    expect(msgs).toMatch(/Seed.*whole/)
  })
  it('requires a name and the required slots', () => {
    const f = formFromModel({ name: '', family: 'moss-image', files: {}, settings: {} }, MOSS)
    const msgs = modelFromForm(f, MOSS).errors
    expect(msgs).toContain('Name is required')
    expect(msgs).toContain('diffusion: a file is required')
    expect(msgs).toContain('tokenizer: a file is required')
  })
  it('requires absolute paths without a leading dash or network prefix', () => {
    const f = good()
    f.files.model = `rel${B}a.safetensors`
    expect(modelFromForm(f, SDXL).errors).toContain('model: path must be absolute')
    f.files.model = '-x.safetensors'
    expect(modelFromForm(f, SDXL).errors.join()).toMatch(/dash/)
    f.files.model = `${B}${B}host${B}share${B}a.safetensors`
    expect(modelFromForm(f, SDXL).errors.join()).toMatch(/network/)
    f.files.model = `C:${B}m${B}a.safetensors`; f.files.vae = '/usr/x.safetensors'
    expect(modelFromForm(f, SDXL).errors).toEqual([])
  })
  it('limits the name to 120 characters and the sampler to the known list', () => {
    const f = good(); f.name = 'x'.repeat(121); f.settings.sampler = 'nope'
    const msgs = modelFromForm(f, SDXL).errors.join('|')
    expect(msgs).toMatch(/120/)
    expect(msgs).toMatch(/Sampler/)
  })
})

describe('after a save', () => {
  it('a form rebuilt from the saved model shows the value the server kept', () => {
    const typed = formFromModel(model, SDXL)
    typed.settings.width = '1030'
    const saved = { ...model, settings: { ...model.settings, width: 1024 } } // the server rounded it
    expect(formFromModel(saved, SDXL).settings.width).toBe('1024')
  })
})

describe('isDirty', () => {
  it('a pristine form is clean and any edit is dirty', () => {
    const base = formFromModel(model, SDXL)
    expect(isDirty(formFromModel(model, SDXL), base)).toBe(false)
    const f = formFromModel(model, SDXL); f.settings.steps = '31'
    expect(isDirty(f, base)).toBe(true)
  })
  it('name and paths kept across a family switch stay dirty against the blank baseline', () => {
    const blank = blankForm('moss-image', MOSS)
    expect(isDirty(blank, blankForm('moss-image', MOSS))).toBe(false)
    const kept = formFromModel({ name: 'Mine', family: 'moss-image', files: { vae: 'C:/v.safetensors' }, settings: {} }, MOSS)
    expect(isDirty(kept, blank)).toBe(true)
    expect(kept.files.vae).toBe('C:/v.safetensors')
    expect(kept.files).not.toHaveProperty('model') // slots the new family lacks are dropped
  })
})

/* ---- Phase 3: video families and LoRAs ---- */
const VIDEO_KEYS = ['frames', 'fps', 'high_noise_steps', 'high_noise_cfg', 'moe_boundary']
const IMAGE_SETTINGS = ['width', 'height', 'steps', 'cfg', 'sampler', 'seed', 'negative', 'offload', 'vae_tiling', 'flash_attn', 'flow_shift']
const SDXL3 = { ...SDXL, kind: 'image', settings: IMAGE_SETTINGS }
const WAN5B = {
  label: 'Wan 2.2 TI2V 5B', kind: 'video', frame_step: 4, needs_init: false,
  slots: [{ name: 'diffusion', required: true }, { name: 't5xxl', required: true }, { name: 'vae', required: true }],
  settings: [...IMAGE_SETTINGS, 'frames', 'fps'],
  defaults: { width: 832, height: 480, frames: 49, fps: 24, steps: 20, cfg: 5, sampler: 'euler', flow_shift: 5, offload: true, vae_tiling: true },
}
const A14B = {
  label: 'Wan 2.2 I2V A14B', kind: 'video', frame_step: 4, needs_init: true,
  slots: [{ name: 'diffusion', required: true }, { name: 'high_noise', required: true },
          { name: 't5xxl', required: true }, { name: 'vae', required: true }],
  settings: [...IMAGE_SETTINGS, ...VIDEO_KEYS],
  defaults: { width: 832, height: 480, frames: 33, fps: 16, steps: 20, cfg: 3.5, sampler: 'euler',
              high_noise_steps: -1, high_noise_cfg: 3.5, moe_boundary: 0.9, flow_shift: 5, offload: true, vae_tiling: true },
}

describe('settingsFor', () => {
  it('an image family gets no video settings', () => {
    const keys = settingsFor(SDXL3).map(f => f.key)
    for (const k of VIDEO_KEYS) expect(keys).not.toContain(k)
    expect(keys).toEqual(settingFields(SDXL).map(f => f.key))
  })
  it('a one-stage video family gets frames and fps, not the high-noise settings', () => {
    const keys = settingsFor(WAN5B).map(f => f.key)
    expect(keys).toContain('frames')
    expect(keys).toContain('fps')
    for (const k of ['high_noise_steps', 'high_noise_cfg', 'moe_boundary']) expect(keys).not.toContain(k)
  })
  it('a two-stage family gets the high-noise settings, labelled', () => {
    const byKey = Object.fromEntries(settingsFor(A14B).map(f => [f.key, f]))
    expect(byKey.frames.label).toBe('Frames')
    expect(byKey.fps.label).toBe('FPS')
    expect(byKey.high_noise_steps.label).toBe('High-noise steps')
    expect(byKey.high_noise_cfg.label).toBe('High-noise CFG')
    expect(byKey.moe_boundary.label).toBe('MoE boundary')
    expect(byKey.high_noise_steps.lo).toBe(-1)
    expect(byKey.moe_boundary.hi).toBe(1)
  })
  it('video sizes stop at 1280', () => {
    expect(settingsFor(WAN5B).find(f => f.key === 'width').hi).toBe(1280)
    expect(settingsFor(SDXL3).find(f => f.key === 'width').hi).toBe(2048)
  })
  it('frames out of range is an error and a video model round-trips', () => {
    const form = formFromModel({ name: 'V', family: 'wan', files: {}, settings: {} }, WAN5B)
    expect(form.settings.frames).toBe('49')
    form.settings.frames = '300'
    expect(modelFromForm(form, WAN5B).errors.join()).toMatch(/Frames.*1.*241/)
    form.settings.frames = '33'
    expect(modelFromForm(form, WAN5B).model.settings).toMatchObject({ frames: 33, fps: 24 })
  })
})

describe('clip length', () => {
  it('is frames over fps in seconds, one decimal', () => {
    expect(clipSeconds(49, 24)).toBe(2)
    expect(clipSeconds('33', '16')).toBe(2.1)
    expect(clipSeconds(81, 16)).toBe(5.1)
  })
  it('is null for missing or impossible values', () => {
    expect(clipSeconds('', 24)).toBeNull()
    expect(clipSeconds(49, 0)).toBeNull()
    expect(clipSeconds('x', 24)).toBeNull()
  })
  it('frames round down to the family step, 1 to 241', () => {
    expect(alignFrames(50, 4)).toBe(49)
    expect(alignFrames(48, 8)).toBe(41)
    expect(alignFrames(300, 4)).toBe(241)
    expect(alignFrames(0, 4)).toBe(1)
    expect(alignFrames(50, 0)).toBe(50)
    expect(alignFrames('x', 4)).toBeNull()
  })
})

describe('LoRAs', () => {
  const dir = `C:${B}loras${B}`
  const row = (o = {}) => ({ file: `${dir}a.safetensors`, strength: '1', stage: 'both', ...o })
  it('stages: high and low only for a two-stage family', () => {
    expect(isTwoStage(A14B)).toBe(true)
    expect(isTwoStage(WAN5B)).toBe(false)
    expect(loraStages(A14B)).toEqual(['both', 'high', 'low'])
    expect(loraStages(WAN5B)).toEqual(['both'])
  })
  it('a good list has no problems', () => {
    expect(loraProblems([row(), row({ file: `${dir}b.safetensors`, stage: 'high', strength: '0.5' })], A14B)).toEqual([])
    expect(loraProblems([], WAN5B)).toEqual([])
  })
  it('flags a missing, relative or non-safetensors file', () => {
    expect(loraProblems([row({ file: ' ' })], WAN5B)).toEqual(['LoRA 1: a file is required'])
    expect(loraProblems([row({ file: 'a.safetensors' })], WAN5B)).toEqual(['LoRA 1: path must be absolute'])
    expect(loraProblems([row({ file: `${dir}a.ckpt` })], WAN5B).join()).toMatch(/LoRA 1:.*pickle/)
    expect(loraProblems([row({ file: `${dir}a.gguf` })], WAN5B).join()).toMatch(/LoRA 1:.*\.safetensors/)
  })
  it('flags a strength that is not a number from 0 to 2', () => {
    expect(loraProblems([row({ strength: 'x' })], WAN5B)).toEqual(['LoRA 1: strength must be a number'])
    expect(loraProblems([row({ strength: '2.5' })], WAN5B)).toEqual(['LoRA 1: strength must be between 0 and 2'])
    expect(loraProblems([row({ strength: '' })], WAN5B)).toEqual(['LoRA 1: strength is required'])
    expect(loraProblems([row({ strength: 2 })], WAN5B)).toEqual([])
  })
  it('flags a high or low stage on a one-stage family', () => {
    expect(loraProblems([row({ stage: 'low' })], WAN5B).join()).toMatch(/LoRA 1:.*high-noise/)
  })
  it('wants every LoRA in one folder', () => {
    expect(loraProblems([row(), row({ file: `D:${B}other${B}b.safetensors` })], WAN5B)).toContain('all LoRAs must be in one folder')
    expect(loraProblems([row(), row({ file: 'c:/loras/b.safetensors' })], WAN5B)).toEqual([])
  })
  it('round-trips through the form and is always sent, even empty', () => {
    const m = { ...model, loras: [{ file: `${dir}a.safetensors`, strength: 0.8, stage: 'both' }] }
    const v = { ...m, family: 'wan2.2-ti2v-5b' }
    const form = formFromModel(v, WAN5B)
    expect(form.loras).toEqual([{ file: `${dir}a.safetensors`, strength: '0.8', stage: 'both' }])
    expect(modelFromForm(form, WAN5B).model.loras).toEqual([{ file: `${dir}a.safetensors`, strength: 0.8, stage: 'both' }])
    expect(modelFromForm(formFromModel({ ...v, loras: [] }, WAN5B), WAN5B).model.loras).toEqual([])
    form.loras[0].strength = '9'
    expect(modelFromForm(form, WAN5B).errors).toContain('LoRA 1: strength must be between 0 and 2')
  })
  it('only video families take LoRAs: a picture model saves with none, clearing any it had', () => {
    expect(takesLoras(WAN5B)).toBe(true)
    expect(takesLoras(A14B)).toBe(true)
    expect(takesLoras(SDXL3)).toBe(false)
    expect(takesLoras(undefined)).toBe(false)
    const m = { ...model, loras: [{ file: `${dir}a.safetensors`, strength: 9, stage: 'high' }] }
    const { model: out, errors } = modelFromForm(formFromModel(m, SDXL3), SDXL3)
    expect(out.loras).toEqual([])
    expect(errors).toEqual([])
    expect(loraProblems([row({ strength: 'x' })], SDXL3)).toEqual([])
  })
})

describe('familyGroups', () => {
  it('groups family ids by kind, pictures first, skipping an empty kind', () => {
    const fams = { sdxl: SDXL3, wan: WAN5B, a14b: A14B, old: { label: 'Old', slots: [], defaults: {} } }
    expect(familyGroups(fams)).toEqual([
      { kind: 'image', label: 'Pictures', ids: ['sdxl', 'old'] },
      { kind: 'video', label: 'Videos', ids: ['wan', 'a14b'] },
    ])
    expect(familyGroups({ sdxl: SDXL3 })).toEqual([{ kind: 'image', label: 'Pictures', ids: ['sdxl'] }])
  })
})
