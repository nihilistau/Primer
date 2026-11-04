import { describe, expect, test } from 'vitest'
import {
  UNKNOWN_PUBLISHER, downloadBlockedReason, errText, rememberLabels, roleWords, formatBytes, formatEta, formatSpeed, groupApprovals, groupByRole,
  autoOpen, itemState, licenceKey, licencesToAccept, planHeld, planTotals, shortSha, withChoice,
} from './setupPlan.js'

const GB = 1e9

describe('formatBytes', () => {
  test('decimal units, one decimal under ten', () => {
    expect(formatBytes(0)).toBe('0 B')
    expect(formatBytes(999)).toBe('999 B')
    expect(formatBytes(1500)).toBe('1.5 KB')
    expect(formatBytes(4100000000)).toBe('4.1 GB')
    expect(formatBytes(28 * GB)).toBe('28 GB')
    expect(formatBytes(812345678912)).toBe('812 GB')
    expect(formatBytes(2.5e12)).toBe('2.5 TB')
  })
  test('unknown sizes say so', () => {
    expect(formatBytes(null)).toBe('unknown')
    expect(formatBytes(undefined)).toBe('unknown')
    expect(formatBytes(-1)).toBe('unknown')
    expect(formatBytes('x')).toBe('unknown')
  })
})

describe('formatSpeed and formatEta', () => {
  test('speed is bytes per second, blank when stopped', () => {
    expect(formatSpeed(12.4e6)).toBe('12 MB/s')
    expect(formatSpeed(1234.5)).toBe('1.2 KB/s')
    expect(formatSpeed(0)).toBe('')
    expect(formatSpeed(null)).toBe('')
  })
  test('eta rounds up and uses the largest units', () => {
    expect(formatEta(12.3)).toBe('13 s left')
    expect(formatEta(59.2)).toBe('60 s left')
    expect(formatEta(61)).toBe('2 min left')
    expect(formatEta(3600)).toBe('1 h left')
    expect(formatEta(3900)).toBe('1 h 5 min left')
    expect(formatEta(null)).toBe('')
    expect(formatEta(-3)).toBe('')
  })
})

test('shortSha keeps the first 12 characters', () => {
  expect(shortSha('a'.repeat(64))).toBe('aaaaaaaaaaaa')
  expect(shortSha('')).toBe('')
  expect(shortSha(null)).toBe('')
})

const plan = {
  kit: 'light', label: 'Light', total: 5 * GB, to_download: 4 * GB, ok: true, skipped: ['zim_top'],
  items: [
    { id: 'writing_model', role: 'text', label: 'LFM3', size: 3 * GB, licence: 'other', licence_link: 'https://l/other', present: false, error: '' },
    { id: 'voice', role: 'voice', label: 'Audio', size: 1 * GB, licence: 'other', licence_link: 'https://l/other', present: true, error: '' },
    { id: 'llama_cpp', role: 'runtime', label: 'llama.cpp', size: 1 * GB, licence: 'MIT', licence_link: 'https://l/mit', present: false, error: '' },
  ],
  licences: [{ licence: 'other', link: 'https://l/other' }, { licence: 'MIT', link: 'https://l/mit' }],
  fits: { ok: true, shortfall_bytes: 0, free_bytes: 100 * GB, needed_bytes: 6 * GB },
}

describe('planTotals', () => {
  test('uses the server’s needed bytes and the free space given', () => {
    expect(planTotals(plan, 100 * GB)).toEqual({ total: 5 * GB, toDownload: 4 * GB, needed: 6 * GB, free: 100 * GB, fits: true, shortfall: 0 })
    expect(planTotals(plan, 5 * GB)).toMatchObject({ fits: false, shortfall: 1 * GB })
  })
  test('falls back to the plan’s own free space, then to the download size', () => {
    expect(planTotals(plan, undefined)).toMatchObject({ free: 100 * GB, fits: true })
    const bare = { ...plan, fits: undefined }
    expect(planTotals(bare, 3 * GB)).toMatchObject({ needed: 4 * GB, fits: false, shortfall: 1 * GB })
    expect(planTotals(bare, null)).toMatchObject({ free: null, fits: null, shortfall: 0 })
  })
  test('no plan is all zeros', () => {
    expect(planTotals(null, 10)).toEqual({ total: 0, toDownload: 0, needed: 0, free: 10, fits: null, shortfall: 0 })
  })
})

describe('itemState', () => {
  test('a present item is already here, before any run', () => {
    expect(itemState({ present: true })).toMatchObject({ label: 'Already here', tone: 'ok', busy: false })
    expect(itemState({ state: 'present' })).toMatchObject({ label: 'Already here', tone: 'ok' })
  })
  test('run states', () => {
    expect(itemState({ state: 'queued' })).toMatchObject({ label: 'Waiting', tone: 'neutral' })
    expect(itemState({ state: 'downloading', bytes: 25, total: 100 })).toMatchObject({ label: 'Downloading', tone: 'accent', busy: true, progress: 0.25 })
    expect(itemState({ state: 'verifying' })).toMatchObject({ label: 'Checking the hash', busy: true })
    expect(itemState({ state: 'extracting' })).toMatchObject({ label: 'Unpacking', busy: true })
    expect(itemState({ state: 'done' })).toMatchObject({ label: 'Done', tone: 'ok', progress: 1 })
    expect(itemState({ state: 'failed', error: 'boom' })).toMatchObject({ label: 'Failed', tone: 'err' })
    expect(itemState({ state: 'skipped' })).toMatchObject({ label: 'Skipped', tone: 'neutral' })
  })
  test('a plan item not yet run, or one the catalogue could not find', () => {
    expect(itemState({ present: false })).toMatchObject({ label: 'To download', tone: 'neutral', progress: null })
    expect(itemState({ present: false, error: 'gone' })).toMatchObject({ label: 'Not found', tone: 'err' })
  })
  test('progress is null without a total and clamped to 0..1', () => {
    expect(itemState({ state: 'downloading', bytes: 5, total: 0 }).progress).toBe(null)
    expect(itemState({ state: 'downloading', bytes: 500, total: 100 }).progress).toBe(1)
  })
})

describe('licencesToAccept', () => {
  const OTHER = { licence: 'other', link: 'https://l/other', key: 'other <https://l/other>' }
  const MIT = { licence: 'MIT', link: 'https://l/mit', key: 'MIT <https://l/mit>' }
  test('the plan’s distinct licences not yet accepted, by name and link', () => {
    expect(licencesToAccept(plan, [])).toEqual([OTHER, MIT])
    expect(licencesToAccept(plan, [OTHER.key])).toEqual([MIT])
    expect(licencesToAccept(plan, new Set([OTHER.key, MIT.key]))).toEqual([])
    expect(licencesToAccept(plan, ['other'])).toEqual([OTHER, MIT])  // a bare name accepts nothing
  })
  test('accepts the state’s licence rows as well', () => {
    expect(licencesToAccept(plan, [{ ...OTHER, accepted: true }, { ...MIT, accepted: false }])).toEqual([MIT])
  })
  test('"other" for two repos is two licences', () => {
    const two = { items: [], licences: [OTHER, { licence: 'other', link: 'https://l/else', key: 'other <https://l/else>' }] }
    expect(licencesToAccept(two, [OTHER.key]).map(l => l.link)).toEqual(['https://l/else'])
  })
  test('without plan.licences, derives them from the items still to download', () => {
    const bare = { ...plan, licences: undefined }
    expect(licencesToAccept(bare, [])).toEqual([OTHER, MIT])
    expect(licencesToAccept(null, [])).toEqual([])
  })
  test('licenceKey matches the server’s rule', () => {
    expect(licenceKey({ licence: 'MIT', link: 'https://l/mit' })).toBe('MIT <https://l/mit>')
    expect(licenceKey({ licence: 'MIT', link: '' })).toBe('MIT')
    expect(licenceKey({ licence: 'MIT', link: 'x', key: 'given' })).toBe('given')
  })
})

describe('planHeld', () => {
  test('only a paused run holds its plan; a failed one can be planned again', () => {
    expect(planHeld({ state: 'paused', plan })).toBe(true)
    expect(planHeld({ state: 'failed', plan })).toBe(false)
    expect(planHeld({ state: 'paused', plan: null })).toBe(false)
    expect(planHeld(null)).toBe(false)
  })
})

describe('downloadBlockedReason', () => {
  const totals = planTotals(plan, 100 * GB)
  test('nothing blocks a good plan with every licence accepted', () => {
    expect(downloadBlockedReason({ plan, totals, toAccept: [] })).toBe('')
  })
  test('a folder edited since the plan was checked needs a new check', () => {
    expect(downloadBlockedReason({ plan, totals, toAccept: [], root: ' /b ', planRoot: '/a' }))
      .toBe('Check the plan again for this folder')
    expect(downloadBlockedReason({ plan, totals, toAccept: [], root: ' /a ', planRoot: '/a' })).toBe('')
    expect(downloadBlockedReason({ plan, totals, toAccept: [], root: '/b', planRoot: '/a', planError: 'bad' }))
      .toBe('The plan has errors: bad')
  })
  test('the reasons, in order', () => {
    expect(downloadBlockedReason({ plan: null })).toBe('Choose a kit and check the plan first')
    expect(downloadBlockedReason({ plan, totals, toAccept: [], planError: 'that is a removable drive' }))
      .toBe('The plan has errors: that is a removable drive')
    expect(downloadBlockedReason({ plan: { ...plan, ok: false }, totals, toAccept: [] }))
      .toBe('The plan has errors: some items could not be found; skip or swap them')
    expect(downloadBlockedReason({ plan: { ...plan, ok: false, error: 'the kit has no writing model' }, totals, toAccept: [] }))
      .toBe('The plan has errors: the kit has no writing model')
    expect(downloadBlockedReason({ plan, totals, toAccept: [], running: true })).toBe('Setup is already downloading')
    expect(downloadBlockedReason({ plan, totals: planTotals(plan, 5 * GB), toAccept: [{ licence: 'MIT' }] }))
      .toBe('Doesn’t fit: about 1.0 GB more space is needed')
    expect(downloadBlockedReason({ plan, totals, toAccept: [{ licence: 'MIT' }, { licence: 'x' }] }))
      .toBe('Licences pending: accept 2 licences first')
    expect(downloadBlockedReason({ plan, totals, toAccept: [{ licence: 'MIT' }] }))
      .toBe('Licences pending: accept 1 licence first')
  })
})

describe('groupApprovals', () => {
  const row = (o) => ({ path: 'p/' + o.file, sha256: 'f'.repeat(64), changed: false, ...o })
  test('Setup’s files by item and release; unknown publishers apart', () => {
    const pending = [
      row({ file: 'llama-server.exe', item: 'llama_cpp', publisher: 'ggml-org/llama.cpp', release: 'b1' }),
      row({ file: 'ggml.dll', item: 'llama_cpp', publisher: 'ggml-org/llama.cpp', release: 'b1' }),
      row({ file: 'sd.exe', item: 'sd_cpp', publisher: 'leejet/stable-diffusion.cpp', release: 'm1' }),
      row({ file: 'version.dll', item: '', publisher: UNKNOWN_PUBLISHER, release: '' }),
    ]
    const g = groupApprovals(pending)
    expect(g.releases.map(r => [r.item, r.publisher, r.release, r.files.map(f => f.file)])).toEqual([
      ['llama_cpp', 'ggml-org/llama.cpp', 'b1', ['llama-server.exe', 'ggml.dll']],
      ['sd_cpp', 'leejet/stable-diffusion.cpp', 'm1', ['sd.exe']],
    ])
    expect(g.unknown.map(f => f.file)).toEqual(['version.dll'])
  })
  test('a row with no item, or any unknown publisher text, is never in a release group', () => {
    const g = groupApprovals([{ file: 'a.exe', item: '', publisher: 'someone', release: 'r' },
                              { file: 'b.dll', item: 'x', publisher: 'unknown — whatever', release: '' }])
    expect(g.releases).toEqual([])
    expect(g.unknown.map(f => f.file)).toEqual(['a.exe', 'b.dll'])
  })
  test('nothing pending', () => {
    expect(groupApprovals(undefined)).toEqual({ releases: [], unknown: [] })
  })
})

describe('groupByRole and withChoice', () => {
  test('items grouped by role, in plan order', () => {
    const g = groupByRole([{ id: 'a', role: 'text' }, { id: 'b', role: 'runtime' }, { id: 'c', role: 'text' }])
    expect(g.map(x => [x.role, x.items.map(i => i.id)])).toEqual([['text', ['a', 'c']], ['runtime', ['b']]])
  })
  test('a key function groups related roles together', () => {
    const fam = (i) => i.role.split('_')[0]
    const g = groupByRole([{ id: 'a', role: 'image' }, { id: 'b', role: 'runtime_llama' }, { id: 'c', role: 'image_vae' }], fam)
    expect(g.map(x => [x.role, x.items.map(i => i.id)])).toEqual([['image', ['a', 'c']], ['runtime', ['b']]])
  })
  test('withChoice sets a choice or skip, and null returns to the default', () => {
    expect(withChoice({ map: 'z9' }, 'zim_top', 'skip')).toEqual({ map: 'z9', zim_top: 'skip' })
    expect(withChoice({ map: 'z9', zim_top: 'skip' }, 'zim_top', null)).toEqual({ map: 'z9' })
    const before = { map: 'z9' }
    withChoice(before, 'map', 'z8')
    expect(before).toEqual({ map: 'z9' })
  })
})

describe('roleWords and skippedLabels', () => {
  test('role ids as words', () => {
    expect(roleWords('runtime_llama')).toBe('llama.cpp runtime')
    expect(roleWords('voice_mmproj')).toBe('voice (audio encoder)')
    expect(roleWords('text')).toBe('writing model')
    expect(roleWords('image: Pic (model)')).toBe('image: Pic (model)')
    expect(roleWords('something_new')).toBe('something new')
  })
  test('skipped items keep the label they had in an earlier plan', () => {
    const known = rememberLabels({}, { items: [{ id: 'zim_top', label: 'Wikipedia' }] })
    expect(known).toEqual({ zim_top: 'Wikipedia' })
    expect(rememberLabels(known, { items: [{ id: 'w', label: 'LFM3' }] })).toEqual({ zim_top: 'Wikipedia', w: 'LFM3' })
    expect(rememberLabels(known, null)).toBe(known)
  })
})

describe('autoOpen', () => {
  const st = (needed, text = 'failed') => ({ setup: { needed, running: false }, servers: [{ role: 'text', state: text }] })
  test('opens once when Setup is needed and it hasn’t been opened', () => {
    expect(autoOpen(st(true), false)).toBe('open')
    expect(autoOpen(st(true), true)).toBe('stop')
  })
  test('keeps watching while the text server may still fail, stops once it is ready', () => {
    expect(autoOpen(st(false, 'starting'), false)).toBe('wait')
    expect(autoOpen(st(false, 'ready'), false)).toBe('stop')
    expect(autoOpen({ servers: [] }, false)).toBe('wait')
    expect(autoOpen(null, false)).toBe('wait')
  })
})

test('errText is the error’s message', () => {
  expect(errText(new Error('not enough space'))).toBe('not enough space')
  expect(errText('plain')).toBe('plain')
})
