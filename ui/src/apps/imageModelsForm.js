/* The Models editor's form <-> model mapping. The checks mirror primer/images (families.py
 * SETTINGS and VIDEO_SETTINGS, models.py _path_problem and the LoRA rules) so a typo shows at
 * once; the server stays authoritative and its 400 text is shown beside the form. */

const SAMPLERS = ['euler', 'euler_a', 'heun', 'dpm2', 'dpm++2s_a', 'dpm++2m', 'lcm']
const MAX_NAME = 120
const VIDEO_MAX_SIZE = 1280 // families.py VIDEO_MAX_SIZE
const MAX_FRAMES = 241
const LORA_MAX_STRENGTH = 2
const LORA_EXT = '.safetensors'
const PICKLE_EXTS = ['.ckpt', '.pt', '.pth', '.bin', '.pkl']
const VIDEO_KEYS = ['frames', 'fps', 'high_noise_steps', 'high_noise_cfg', 'moe_boundary']
const TWO_STAGE_KEYS = ['high_noise_steps', 'high_noise_cfg', 'moe_boundary']

// ordered as the editor shows them; kind: int | float | text | sampler | bool
const FIELDS = [
  { key: 'width', label: 'Width', kind: 'int', lo: 256, hi: 2048, hint: 'pixels, 256 to 2048' },
  { key: 'height', label: 'Height', kind: 'int', lo: 256, hi: 2048, hint: 'pixels, 256 to 2048' },
  { key: 'frames', label: 'Frames', kind: 'int', lo: 1, hi: MAX_FRAMES, hint: 'how many frames, 1 to 241' },
  { key: 'fps', label: 'FPS', kind: 'int', lo: 1, hi: 60, hint: 'frames a second, 1 to 60' },
  { key: 'steps', label: 'Steps', kind: 'int', lo: 1, hi: 150, hint: 'more is slower and finer, 1 to 150' },
  { key: 'cfg', label: 'CFG', kind: 'float', lo: 0, hi: 30, hint: 'how closely to follow the prompt, 0 to 30' },
  { key: 'high_noise_steps', label: 'High-noise steps', kind: 'int', lo: -1, hi: 150, hint: 'steps for the high-noise model, -1 to let it choose' },
  { key: 'high_noise_cfg', label: 'High-noise CFG', kind: 'float', lo: 0, hi: 30, hint: 'CFG for the high-noise model, 0 to 30' },
  { key: 'moe_boundary', label: 'MoE boundary', kind: 'float', lo: 0, hi: 1, hint: 'where the low-noise model takes over, 0 to 1' },
  { key: 'sampler', label: 'Sampler', kind: 'sampler', options: SAMPLERS, hint: 'the denoising method' },
  { key: 'seed', label: 'Seed', kind: 'int', lo: -1, hi: 2 ** 31 - 1, optional: true, hint: 'blank or -1 for a new one each time' },
  { key: 'negative', label: 'Avoid', kind: 'text', max: 1000, optional: true, hint: 'things to keep out of the picture' },
  { key: 'offload', label: 'Offload to CPU', kind: 'bool', hint: 'slower, but fits smaller graphics cards' },
  { key: 'vae_tiling', label: 'Tile the VAE', kind: 'bool', hint: 'saves memory on large pictures' },
  { key: 'flash_attn', label: 'Flash attention', kind: 'bool', hint: 'faster on cards that support it' },
  { key: 'flow_shift', label: 'Flow shift', kind: 'float', lo: 0, hi: 20, optional: true, hint: '0 to 20' },
]

export const kindOf = (family) => (family && family.kind) || 'image'

/* LoRAs are only used by video models (models.py LORA_VIDEO_ONLY) */
export const takesLoras = (family) => kindOf(family) === 'video'

/* a high-noise and a low-noise model (Wan 2.2 A14B): LoRAs can target either stage */
export const isTwoStage = (family) => !!family && (family.slots || []).some(s => s.name === 'high_noise')

/* the setting fields this family takes, in editor order. The server's list of valid keys
 * (family.settings) decides; without it, an image family's keys. Flow shift shows only for a
 * family that has a default for it, as in Phase 2. */
export function settingsFor(family) {
  const defaults = (family && family.defaults) || {}
  const video = kindOf(family) === 'video'
  const valid = family && Array.isArray(family.settings) ? family.settings
    : FIELDS.map(f => f.key).filter(k => !VIDEO_KEYS.includes(k)
      || (video && (!TWO_STAGE_KEYS.includes(k) || isTwoStage(family))))
  const fields = FIELDS.filter(f => valid.includes(f.key) && (f.key !== 'flow_shift' || 'flow_shift' in defaults))
  return fields.map(f => {
    if (video && (f.key === 'width' || f.key === 'height')) {
      return { ...f, hi: VIDEO_MAX_SIZE, hint: `pixels, 256 to ${VIDEO_MAX_SIZE}` }
    }
    if (f.key === 'frames' && family && family.frame_step) {
      return { ...f, hint: `rounded down to ${family.frame_step}n+1, 1 to ${MAX_FRAMES}` }
    }
    return f
  })
}

export const settingFields = settingsFor // the Phase 2 name

/* frames as the server keeps them: 1..241, rounded down to step*n + 1; null when not a number */
export function alignFrames(frames, step) {
  const raw = typeof frames === 'string' ? frames.trim() : frames
  if (raw === '' || raw === null || raw === undefined) return null
  let n = Math.floor(Number(raw))
  if (!Number.isFinite(n)) return null
  n = Math.max(1, Math.min(MAX_FRAMES, n))
  if (step) n -= (n - 1) % step
  return n
}

/* a clip's length in seconds, one decimal; null when frames or fps can't make one */
export function clipSeconds(frames, fps) {
  const num = (v) => (typeof v === 'string' ? (v.trim() === '' ? NaN : Number(v)) : Number(v ?? NaN))
  const f = num(frames), r = num(fps)
  if (!Number.isFinite(f) || !Number.isFinite(r) || f < 1 || r <= 0) return null
  return Math.round((f / r) * 10) / 10
}

/* family ids grouped by kind for the New model picker, pictures first */
export function familyGroups(families) {
  const groups = [{ kind: 'image', label: 'Pictures', ids: [] }, { kind: 'video', label: 'Videos', ids: [] }]
  for (const [id, fam] of Object.entries(families || {})) {
    (groups.find(g => g.kind === kindOf(fam)) || groups[0]).ids.push(id)
  }
  return groups.filter(g => g.ids.length)
}

const show = (v) => (v === undefined || v === null ? '' : typeof v === 'boolean' ? v : String(v))

export function formFromModel(model, family) {
  const merged = { ...((family && family.defaults) || {}), ...(model.settings || {}) }
  const settings = {}
  for (const f of settingsFor(family)) settings[f.key] = f.kind === 'bool' ? !!merged[f.key] : show(merged[f.key])
  const files = {}
  for (const s of (family && family.slots) || []) files[s.name] = (model.files || {})[s.name] || ''
  const loras = (model.loras || []).map(l => ({
    file: l.file || '', strength: show(l.strength ?? 1), stage: l.stage || 'both',
  }))
  return { id: model.id, name: model.name || '', family: model.family, files, settings, loras }
}

const BS = String.fromCharCode(92)
const isSep = (c) => c === '/' || c === BS

function pathProblem(p) {
  if (p.startsWith('-')) return 'path must not start with a dash'
  if (isSep(p[0]) && isSep(p[1])) return "network and device paths aren't allowed"
  const drive = /^[A-Za-z]:/.test(p) && isSep(p[2])
  if (!drive && p[0] !== '/') return 'path must be absolute'
  return null
}

/* LoRA stages the family offers: high and low only with a high-noise stage */
export const loraStages = (family) => (isTwoStage(family) ? ['both', 'high', 'low'] : ['both'])
export const STAGE_LABELS = { both: 'Both stages', high: 'High noise', low: 'Low noise' }

const ext = (p) => { const m = /\.[^./\\]+$/.exec(p); return m ? m[0].toLowerCase() : '' }
const folderOf = (p) => p.replace(/[\\/]+/g, '/').replace(/\/[^/]*$/, '').toLowerCase()

function parseStrength(raw) {
  if (typeof raw === 'number') return raw
  const s = typeof raw === 'string' ? raw.trim() : ''
  return s === '' ? undefined : Number(s)
}

/* what's wrong with the form's LoRA rows, as "LoRA n: ..." lines (models.py clean_loras and
 * _lora_reasons); empty means they can be saved */
export function loraProblems(loras, family) {
  const out = []
  if (!takesLoras(family)) return out  // not sent for a picture model (modelFromForm)
  const folders = new Set()
  const stages = loraStages(family)
  ;(loras || []).forEach((l, i) => {
    const n = `LoRA ${i + 1}`
    const file = (l.file || '').trim()
    if (!file) out.push(`${n}: a file is required`)
    else {
      const problem = pathProblem(file)
      if (problem) out.push(`${n}: ${problem}`)
      else if (PICKLE_EXTS.includes(ext(file))) out.push(`${n}: ${ext(file)} refused, pickle files can run code when loaded`)
      else if (ext(file) !== LORA_EXT) out.push(`${n}: use a ${LORA_EXT} file`)
      else folders.add(folderOf(file))
    }
    const s = parseStrength(l.strength)
    if (s === undefined) out.push(`${n}: strength is required`)
    else if (!Number.isFinite(s)) out.push(`${n}: strength must be a number`)
    else if (s < 0 || s > LORA_MAX_STRENGTH) out.push(`${n}: strength must be between 0 and ${LORA_MAX_STRENGTH}`)
    const stage = l.stage || 'both'
    if (!stages.includes(stage)) {
      out.push(['both', 'high', 'low'].includes(stage)
        ? `${n}: stage high or low needs a model with a high-noise stage`
        : `${n}: stage must be one of ${stages.join(', ')}`)
    }
  })
  if (folders.size > 1) out.push('all LoRAs must be in one folder') // sd-server takes one --lora-model-dir
  return out
}

export const blankLora = () => ({ file: '', strength: '1', stage: 'both' })

function parseField(f, raw, errors) {
  if (f.kind === 'bool') return raw === true
  const s = typeof raw === 'string' ? raw.trim() : ''
  if (f.kind === 'text') {
    if (s.length > f.max) errors.push(`${f.label} must be at most ${f.max} characters`)
    return s || undefined
  }
  if (f.kind === 'sampler') {
    if (!SAMPLERS.includes(s)) errors.push(`Sampler must be one of ${SAMPLERS.join(', ')}`)
    return s
  }
  if (s === '') {
    if (!f.optional) errors.push(`${f.label} is required`)
    return undefined
  }
  const n = Number(s)
  if (!Number.isFinite(n)) { errors.push(`${f.label} must be a number`); return undefined }
  if (f.kind === 'int' && !Number.isInteger(n)) { errors.push(`${f.label} must be a whole number`); return undefined }
  if (n < f.lo || n > f.hi) { errors.push(`${f.label} must be between ${f.lo} and ${f.hi}`); return undefined }
  return n
}

export function modelFromForm(form, family) {
  const errors = []
  const name = (form.name || '').trim()
  if (!name) errors.push('Name is required')
  else if (name.length > MAX_NAME) errors.push(`Name must be at most ${MAX_NAME} characters`)

  const files = {}
  for (const s of (family && family.slots) || []) {
    const p = ((form.files || {})[s.name] || '').trim()
    if (!p) {
      if (s.required) errors.push(`${s.name}: a file is required`)
      continue
    }
    const problem = pathProblem(p)
    if (problem) errors.push(`${s.name}: ${problem}`)
    files[s.name] = p
  }

  const settings = {}
  for (const f of settingsFor(family)) {
    const v = parseField(f, (form.settings || {})[f.key], errors)
    if (v !== undefined) settings[f.key] = v
  }

  errors.push(...loraProblems(form.loras, family))
  // always sent: a save without loras clears them, and a picture model is saved without any
  const loras = (takesLoras(family) ? form.loras || [] : []).map(l => ({
    file: (l.file || '').trim(), strength: parseStrength(l.strength), stage: l.stage || 'both',
  }))

  const model = { name, family: form.family, files, settings, loras }
  if (form.id) model.id = form.id
  return { model, errors }
}

// the empty form for a new model of this family: the baseline "unsaved" is measured against
export const blankForm = (familyId, family) => formFromModel({ name: '', family: familyId, files: {}, settings: {} }, family)

export const isDirty = (form, baseline) => JSON.stringify(form) !== JSON.stringify(baseline)
