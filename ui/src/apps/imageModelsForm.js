/* The image-model editor's form <-> model mapping. The checks mirror primer/images (families.py
 * SETTINGS, models.py _path_problem) so a typo shows at once; the server stays authoritative and
 * its 400 text is shown beside the form. */

const SAMPLERS = ['euler', 'euler_a', 'heun', 'dpm2', 'dpm++2s_a', 'dpm++2m', 'lcm']
const MAX_NAME = 120

// ordered as the editor shows them; kind: int | float | text | sampler | bool
const FIELDS = [
  { key: 'width', label: 'Width', kind: 'int', lo: 256, hi: 2048, hint: 'pixels, 256 to 2048' },
  { key: 'height', label: 'Height', kind: 'int', lo: 256, hi: 2048, hint: 'pixels, 256 to 2048' },
  { key: 'steps', label: 'Steps', kind: 'int', lo: 1, hi: 150, hint: 'more is slower and finer, 1 to 150' },
  { key: 'cfg', label: 'CFG', kind: 'float', lo: 0, hi: 30, hint: 'how closely to follow the prompt, 0 to 30' },
  { key: 'sampler', label: 'Sampler', kind: 'sampler', options: SAMPLERS, hint: 'the denoising method' },
  { key: 'seed', label: 'Seed', kind: 'int', lo: -1, hi: 2 ** 31 - 1, optional: true, hint: 'blank or -1 for a new one each time' },
  { key: 'negative', label: 'Avoid', kind: 'text', max: 1000, optional: true, hint: 'things to keep out of the picture' },
  { key: 'offload', label: 'Offload to CPU', kind: 'bool', hint: 'slower, but fits smaller graphics cards' },
  { key: 'vae_tiling', label: 'Tile the VAE', kind: 'bool', hint: 'saves memory on large pictures' },
  { key: 'flash_attn', label: 'Flash attention', kind: 'bool', hint: 'faster on cards that support it' },
  { key: 'flow_shift', label: 'Flow shift', kind: 'float', lo: 0, hi: 20, optional: true, hint: 'MOSS only, 0 to 20' },
]

export function settingFields(family) {
  const defaults = (family && family.defaults) || {}
  return FIELDS.filter(f => f.key !== 'flow_shift' || 'flow_shift' in defaults)
}

const show = (v) => (v === undefined || v === null ? '' : typeof v === 'boolean' ? v : String(v))

export function formFromModel(model, family) {
  const merged = { ...((family && family.defaults) || {}), ...(model.settings || {}) }
  const settings = {}
  for (const f of settingFields(family)) settings[f.key] = f.kind === 'bool' ? !!merged[f.key] : show(merged[f.key])
  const files = {}
  for (const s of (family && family.slots) || []) files[s.name] = (model.files || {})[s.name] || ''
  return { id: model.id, name: model.name || '', family: model.family, files, settings }
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
  for (const f of settingFields(family)) {
    const v = parseField(f, (form.settings || {})[f.key], errors)
    if (v !== undefined) settings[f.key] = v
  }

  const model = { name, family: form.family, files, settings }
  if (form.id) model.id = form.id
  return { model, errors }
}

// the empty form for a new model of this family: the baseline "unsaved" is measured against
export const blankForm = (familyId, family) => formFromModel({ name: '', family: familyId, files: {}, settings: {} }, family)

export const isDirty = (form, baseline) => JSON.stringify(form) !== JSON.stringify(baseline)
