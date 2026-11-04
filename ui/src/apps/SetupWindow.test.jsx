/* the Setup window renders each of its states (server-side render: no DOM needed) */
import { describe, expect, test } from 'vitest'
import { renderToString } from 'react-dom/server'
import { Install } from './SetupWindow.jsx'

const GB = 1e9
const plan = {
  kit: 'light', label: 'Light', total: 5 * GB, to_download: 4 * GB, ok: true, skipped: ['zim_top'], checked: '2026-11-04',
  items: [
    { id: 'w', role: 'text', label: 'LFM3', size: 3 * GB, licence: 'other', licence_link: 'https://example.org/l', present: false,
      gated: 'auto', choice: 'q4', choices: [{ id: 'q4', label: 'Q4', size: 1 }, { id: 'q8', label: 'Q8', size: 2 }], error: '' },
    { id: 'v', role: 'voice_mmproj', label: 'Audio', size: GB, licence: 'other', present: true, choices: [], error: '' },
  ],
  licences: [{ licence: 'other', link: 'https://example.org/l' }],
}
const running = {
  state: 'paused', kit: 'light', root: '/data/primer', choices: {}, plan, tier: 'local-light', error: '', models_added: ['Pic'],
  items: [{ id: 'w', label: 'LFM3', state: 'downloading', bytes: GB, total: 3 * GB, speed_bps: 5e6, eta_s: 400, error: '' }],
  licences: [{ licence: 'other', link: 'https://example.org/l', accepted: false }],
  approvals: [
    { path: 'p/a.exe', sha256: 'a'.repeat(64), publisher: 'ggml-org/llama.cpp', release: 'b1', file: 'a.exe', item: 'llama_cpp', changed: false },
    { path: 'p/v.dll', sha256: 'b'.repeat(64), publisher: 'unknown — not installed by Setup', release: '', file: 'v.dll', item: '', changed: false },
  ],
}
const data = {
  hardware: { gpu: 'G', vram_gb: 12 }, suggested: { kit: 'hybrid', reason: '12 GB GPU (G) → Hybrid' },
  kits: [{ id: 'light', label: 'Light', summary: 's', size_hint: 4.1e9 }, { id: 'hybrid', label: 'Hybrid', summary: 'h', size_hint: 28e9 }],
  root_default: '/data/primer', state: running, free_bytes: 100 * GB, token: false,
}
const text = (st) => renderToString(<Install data={{ ...data, state: st }} refresh={() => {}} />).replace(/<[^>]+>/g, ' ').replace(/\s+/g, ' ')

describe('Install', () => {
  test('before a plan: the kits, suggested first with its reason, and the destination', () => {
    const t = text({ state: 'idle', items: [], licences: [], approvals: [] })
    expect(t.indexOf('Hybrid')).toBeLessThan(t.indexOf('Light'))
    expect(t).toContain('12 GB GPU (G) → Hybrid')
    expect(t).toContain('100 GB free')
    expect(t).not.toContain('Download')
  })
  test('a paused run: plan, licences, progress and approvals, with no Download button', () => {
    const t = text(running)
    expect(t).toContain('Already here')
    expect(t).toContain('No Hugging Face token found')
    expect(t).toContain('1 still to accept')
    expect(t).toContain('1.0 GB of 3.0 GB · 5.0 MB/s · 7 min left')
    expect(t).toContain('Approve all from this release')
    expect(t).toContain('not installed by Setup')
    expect(t).not.toContain('Download ')
    expect(t).toContain('SHA-256 ' + 'a'.repeat(64))
    expect(t).toContain('held by the paused run')
    expect(t).toContain('zim_top')
  })
  test('a failed run does not hold its plan: the kit and folder can change again', () => {
    const t = text({ ...running, state: 'failed', error: 'the tier could not be written' })
    expect(t).not.toContain('held by')
  })
  test('an idle plan with a licence pending says why Download is disabled', () => {
    const t = text({ ...running, state: 'idle', items: [], approvals: [] })
    expect(t).toContain('Licences pending: accept 1 licence first')
  })
  test('ready offers the new tier', () => {
    const t = text({ ...running, state: 'ready', approvals: [] })
    expect(t).toContain('Switch to the new tier')
    expect(t).toContain('local-light')
    expect(t).toContain('Pic')
    expect(t).not.toContain('Download ')
  })
})
