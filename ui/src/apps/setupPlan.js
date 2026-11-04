/* setupPlan — the Setup window's pure logic: sizes, speeds and times as words, a plan's
 * totals against the free space, an item's state as a chip, which licences are still to
 * accept, why Download is disabled, and the pending executables grouped by release. The
 * server is authoritative for every check; these only say what it will say, sooner. */

/* approvals.UNKNOWN_PUBLISHER: a file Setup didn't install, found beside one it did */
export const UNKNOWN_PUBLISHER = 'unknown — not installed by Setup'

const UNITS = ['B', 'KB', 'MB', 'GB', 'TB', 'PB']

/* decimal units (disk makers' and the publishers' own), one decimal under ten */
export function formatBytes(n) {
  if (typeof n !== 'number' || !Number.isFinite(n) || n < 0) return 'unknown'
  let i = 0
  while (n >= 1000 && i < UNITS.length - 1) { n /= 1000; i++ }
  if (i === 0) return `${Math.round(n)} B`
  return `${n < 10 ? n.toFixed(1) : Math.round(n)} ${UNITS[i]}`
}

export const formatSpeed = (bps) => (typeof bps === 'number' && bps > 0 ? `${formatBytes(bps)}/s` : '')

export function formatEta(s) {
  if (typeof s !== 'number' || !Number.isFinite(s) || s < 0) return ''
  s = Math.ceil(s)
  if (s <= 60) return `${s} s left`
  if (s < 3600) return `${Math.ceil(s / 60)} min left`
  let h = Math.floor(s / 3600)
  let m = Math.ceil((s - h * 3600) / 60)
  if (m === 60) { h++; m = 0 }
  return m ? `${h} h ${m} min left` : `${h} h left`
}

export const shortSha = (sha) => (typeof sha === 'string' ? sha.slice(0, 12) : '')

/* What the plan downloads and needs against the free space. `needed` is the server's
 * figure (downloads, unpacking room, a margin) when the plan carries one. `free` falls back
 * to the plan's own reading; `fits` is null while the free space is unknown. */
export function planTotals(plan, free) {
  if (free === undefined) free = plan && plan.fits ? plan.fits.free_bytes : null
  if (typeof free !== 'number') free = null
  if (!plan) return { total: 0, toDownload: 0, needed: 0, free, fits: null, shortfall: 0 }
  const total = plan.total || 0
  const toDownload = plan.to_download || 0
  const needed = plan.fits && typeof plan.fits.needed_bytes === 'number' ? plan.fits.needed_bytes : toDownload
  const fits = free === null ? null : free >= needed
  return { total, toDownload, needed, free, fits, shortfall: fits === false ? needed - free : 0 }
}

const STATES = {
  queued: { label: 'Waiting', tone: 'neutral' },
  downloading: { label: 'Downloading', tone: 'accent', busy: true },
  verifying: { label: 'Checking the hash', tone: 'accent', busy: true },
  extracting: { label: 'Unpacking', tone: 'accent', busy: true },
  done: { label: 'Done', tone: 'ok' },
  failed: { label: 'Failed', tone: 'err' },
  skipped: { label: 'Skipped', tone: 'neutral' },
  present: { label: 'Already here', tone: 'ok' },
}

/* An item (a plan item, a run item, or the two merged) as a chip: label, tone, busy, and
 * progress (0..1, or null when there is no total to measure against). */
export function itemState(item) {
  const it = item || {}
  let s
  if (it.state && STATES[it.state]) s = STATES[it.state]
  else if (it.present) s = STATES.present
  else if (it.error) s = { label: 'Not found', tone: 'err' }
  else s = { label: 'To download', tone: 'neutral' }
  let progress = null
  if (it.state === 'done' || it.state === 'present') progress = 1
  else if (typeof it.total === 'number' && it.total > 0) progress = Math.min(1, Math.max(0, (it.bytes || 0) / it.total))
  return { label: s.label, tone: s.tone, busy: !!s.busy, progress }
}

/* One set of terms: the licence name and where its text is ("other" for two repos is two).
 * The server's own `key` when it sent one (primer.setup.kits.licence_key, the same rule). */
export function licenceKey(l) {
  if (!l) return ''
  if (typeof l.key === 'string' && l.key) return l.key
  return l.link ? `${l.licence} <${l.link}>` : (l.licence || '')
}

function acceptedSet(accepted) {
  if (accepted instanceof Set) return accepted
  const out = new Set()
  for (const a of accepted || []) {
    if (typeof a === 'string') out.add(a)
    else if (a && a.accepted) out.add(licenceKey(a))
  }
  return out
}

/* The plan's distinct licences ({licence, link, key}) not yet accepted. `accepted` is a Set
 * or an array of licence keys, or the state's [{licence, link, key, accepted}] rows. */
export function licencesToAccept(plan, accepted) {
  if (!plan) return []
  let all = plan.licences
  if (!Array.isArray(all)) {
    const seen = new Map()
    for (const i of plan.items || []) {
      if (i.present || !i.licence) continue
      const l = { licence: i.licence, link: i.licence_link || '' }
      if (!seen.has(licenceKey(l))) seen.set(licenceKey(l), l)
    }
    all = [...seen.values()]
  }
  const ok = acceptedSet(accepted)
  return all.filter(l => !ok.has(licenceKey(l))).map(l => ({ licence: l.licence, link: l.link, key: licenceKey(l) }))
}

/* A paused run holds its plan (the server refuses a new one until it is resumed or done); a
 * failed one doesn't: nothing is running, so the kit, folder and items can change again. */
export const planHeld = (st) => !!st && st.state === 'paused' && !!st.plan

/* Why Download is disabled, as one sentence; '' when nothing stops it. */
export function downloadBlockedReason({ plan, totals, toAccept, running, planError, root, planRoot } = {}) {
  if (!plan) return 'Choose a kit and check the plan first'
  if (planError) return `The plan has errors: ${planError}`
  // the plan (and its free space) is for the folder it was checked with, not one typed since
  if (typeof root === 'string' && typeof planRoot === 'string' && root.trim() !== planRoot.trim()) {
    return 'Check the plan again for this folder'
  }
  if (plan.ok === false) {
    return plan.error ? `The plan has errors: ${plan.error}`
      : 'The plan has errors: some items could not be found; skip or swap them'
  }
  if (running) return 'Setup is already downloading'
  if (totals && totals.fits === false) return `Doesn’t fit: about ${formatBytes(totals.shortfall)} more space is needed`
  const n = (toAccept || []).length
  if (n) return `Licences pending: accept ${n} licence${n === 1 ? '' : 's'} first`
  return ''
}

const unknownRow = (r) => !r.item || typeof r.publisher !== 'string' || r.publisher.startsWith('unknown')

/* Pending executables: Setup's own by item and release (approved together), and the files
 * Setup didn't install (approved one at a time, never with a release). */
export function groupApprovals(pending) {
  const releases = []
  const byKey = new Map()
  const unknown = []
  for (const r of pending || []) {
    if (unknownRow(r)) { unknown.push(r); continue }
    const key = `${r.item}\u0000${r.release}`
    let g = byKey.get(key)
    if (!g) {
      g = { key, item: r.item, publisher: r.publisher, release: r.release, files: [] }
      byKey.set(key, g); releases.push(g)
    }
    g.files.push(r)
  }
  return { releases, unknown }
}

/* plan items grouped by role (or by keyOf(item)), groups in the order they first appear */
export function groupByRole(items, keyOf = (i) => i.role) {
  const out = []
  const by = new Map()
  for (const i of items || []) {
    const role = keyOf(i)
    let g = by.get(role)
    if (!g) { g = { role, items: [] }; by.set(role, g); out.push(g) }
    g.items.push(i)
  }
  return out
}

/* a new choices map with one item set to a choice id or "skip"; null drops it (the default) */
export function withChoice(choices, id, value) {
  const out = { ...(choices || {}) }
  if (value === null || value === undefined) delete out[id]
  else out[id] = value
  return out
}

/* First run: given /v1/status and whether Setup was opened this session, 'open' it once when
 * it is needed, 'stop' watching once opened or once the text server is ready, else 'wait'
 * (status.setup.needed only turns true after a start has failed, so the shell keeps asking). */
export function autoOpen(status, opened) {
  if (opened) return 'stop'
  if (status && status.setup && status.setup.needed) return 'open'
  const text = status && (status.servers || []).find(s => s.role === 'text')
  return text && text.state === 'ready' ? 'stop' : 'wait'
}

/* an error as the one sentence the server sent */
export const errText = (e) => String((e && e.message) || e)

const ROLE_WORDS = {
  text: 'writing model', fast: 'fast jobs model', text_mmproj: 'writing (vision encoder)',
  voice: 'voice model', voice_mmproj: 'voice (audio encoder)', reference: 'reference library', map: 'world map',
  image: 'picture model', image_encoder: 'picture (text encoder)', image_vae: 'picture (VAE)',
  video: 'video model', video_encoder: 'video (text encoder)', video_vae: 'video (VAE)',
  runtime_llama: 'llama.cpp runtime', runtime_llama_cudart: 'llama.cpp CUDA libraries',
  runtime_sd: 'stable-diffusion.cpp runtime', runtime_sd_cudart: 'stable-diffusion.cpp CUDA libraries',
  runtime_kiwix: 'kiwix-tools', fonts: 'map fonts',
}

/* a role id as words; the mover's "image: <model> (<slot>)" roles are already words */
export function roleWords(role) {
  const r = String(role || '')
  if (ROLE_WORDS[r]) return ROLE_WORDS[r]
  return r.includes(':') ? r : r.replace(/_/g, ' ')
}

/* item id -> label, kept across plans: a skipped item is only an id in the next plan */
export function rememberLabels(known, plan) {
  const fresh = ((plan && plan.items) || []).filter(i => i.label && known[i.id] !== i.label)
  if (!fresh.length) return known
  const out = { ...known }
  for (const i of fresh) out[i.id] = i.label
  return out
}
