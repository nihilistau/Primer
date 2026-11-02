import { describe, expect, it } from 'vitest'
import { blankForm, formFromModel, isDirty, modelFromForm, settingFields } from './imageModelsForm.js'

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
