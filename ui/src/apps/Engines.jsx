/* Engines — which tier is chosen, whether each model server is up (with Start/Stop), and
 * how the book is sourced. Polls every 2 s. */
import { useState } from 'react'
import * as api from '../api.js'
import { rewriteOptions } from '../reader/sourceMode.js'
import { Button, Chip, Select } from '../kit/parts.jsx'
import { Body, usePoll } from './panel.jsx'
import './apps.css'

const TONE = { ready: 'ok', unavailable: 'warn', busy: 'warn', starting: 'warn', failed: 'err', external: 'neutral', stopped: 'neutral' }

function hardwareNote(hw) {
  if (!hw || !hw.gpu_name) return 'suggested for this machine (no NVIDIA GPU found)'
  return `suggested for your ${hw.gpu_name}, ${hw.vram_gb} GB`
}

function Server({ s, busy, onAction }) {
  const hint = s.state === 'external' ? 'running outside Primer' : ''
  return (
    <div className="en-server">
      <div className="en-server-top">
        <span className="en-role">{s.role === 'reference' ? 'Reference server' : s.role}</span>
        <Chip tone={TONE[s.state] || 'neutral'} dot>{s.state}</Chip>
        <span className="en-actions">
          <Button size="sm" disabled={busy || ['ready', 'starting', 'external'].includes(s.state)}
                  onClick={() => onAction(s.role, 'start')}>Start</Button>
          <Button size="sm" disabled={busy || ['stopped', 'external'].includes(s.state)} title={hint || undefined}
                  onClick={() => onAction(s.role, 'stop')}>Stop</Button>
        </span>
      </div>
      <div className="en-detail">{[s.model, s.base_url].filter(Boolean).join(' · ')}</div>
      {s.state !== 'ready' && (s.reason || hint) ? <div className="en-reason">{s.reason || hint}</div> : null}
    </div>
  )
}

function ImageServer({ im, avail, gpu }) {
  const name = im.model_name || im.model_id || null
  const reason = (im && im.reason) || (avail && !avail.ok ? avail.reason : '')
  return (
    <div className="en-server" data-role="image-server">
      <div className="en-server-top">
        <span className="en-role">Image server</span>
        <Chip tone={TONE[im.state] || 'neutral'} dot>{im.state}</Chip>
      </div>
      {name ? <div className="en-detail">{name}</div> : null}
      {im.state !== 'ready' && reason ? <div className="en-reason">{reason}</div> : null}
      {gpu && gpu.mode === 'auto' && gpu.text_paused
        ? <div className="en-paused">Writing model paused for a picture or video</div> : null}
    </div>
  )
}

export default function Engines() {
  const poll = usePoll(api.status, 2000)
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState('')

  // run a change, then re-read at once rather than waiting for the next tick
  const act = async (fn) => {
    setBusy(true); setErr('')
    try { await fn() } catch (e) { setErr(String(e.message || e)) }
    setBusy(false)
    poll.refresh()
  }

  return (
    <div className="en">
      <Body state={poll}>
        {(st) => (
          <>
            <section className="en-sec">
              <h3 className="en-h">Tier</h3>
              <div className="en-tier">
                <Chip tone="accent">{st.tier}</Chip>
                <Select aria-label="Tier" value={st.tier} disabled={busy}
                        onChange={(e) => act(() => api.setTier(e.target.value))}>
                  {st.tiers.map(t => <option key={t} value={t}>{t === st.suggested_tier ? `${t} (suggested)` : t}</option>)}
                </Select>
              </div>
              <div className="en-note">{hardwareNote(st.hardware)}</div>
            </section>
            <section className="en-sec">
              <h3 className="en-h">Model servers</h3>
              {st.servers.map(s => (
                <Server key={s.role} s={s} busy={busy} onAction={(role, a) => act(() => api.serverAction(role, a))} />
              ))}
              {st.images ? <ImageServer im={st.images} avail={st.image_available} gpu={st.gpu} /> : null}
              {err ? <div className="en-err" role="alert">{err}</div> : null}
            </section>
            <section className="en-sec">
              <h3 className="en-h">Modes</h3>
              <div className="en-modes">
              <div className="en-tier"><span>Source</span>
                <Select aria-label="Source" value={st.source_mode} disabled={busy}
                        onChange={(e) => act(() => api.setSourceMode(e.target.value))}>
                  {rewriteOptions(st).map(o => (
                    <option key={o.mode} value={o.mode} disabled={o.disabled && o.mode !== st.source_mode}
                            title={o.disabled ? o.reason : undefined}>
                      {o.disabled ? `${o.label} (${o.reason})` : o.label}
                    </option>
                  ))}
                </Select></div>
              <div className="en-tier"><span>GPU</span><Chip>{st.gpu_mode}</Chip>
                <span className="en-note">configurable per tier</span></div>
              </div>
            </section>
          </>
        )}
      </Body>
    </div>
  )
}
