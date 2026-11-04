/* Move models: the configured model files with their drive (removable or slow ones flagged),
 * choose some and a folder, Move (copied, verified, paths updated, originals kept). Polls
 * once a second only while a move runs. */
import { useEffect, useState } from 'react'
import * as api from '../../api.js'
import { Button, Chip, Input } from '../../kit/parts.jsx'
import { Body, usePoll } from '../panel.jsx'
import { errText, formatBytes, roleWords } from '../setupPlan.js'

const MOVE_TONE = { running: 'accent', done: 'ok', cancelled: 'warn', failed: 'err' }

function Result({ res }) {
  if (!res) return null
  return (
    <div className="su-result">
      {(res.moved || []).length ? <div>Moved {res.moved.length} file{res.moved.length === 1 ? '' : 's'}.</div> : null}
      {(res.failed || []).map(f => <div key={f.path} className="en-err">{f.path}: {f.error}</div>)}
      {(res.tiers_updated || []).length ? <div className="en-note">Tiers updated: {res.tiers_updated.join(', ')}</div> : null}
      {(res.images_updated || []).length ? <div className="en-note">Models updated: {res.images_updated.join(', ')}</div> : null}
      {res.reloaded ? <div className="en-note">Reloaded the tier {res.reloaded}.</div> : null}
      {(res.other_tiers || []).map(o => (
        <div key={o.tier + o.path} className="en-note">The tier {o.tier} also names {o.path}; it was left as it is.</div>
      ))}
    </div>
  )
}

function Listing({ data, refresh }) {
  const [picked, setPicked] = useState(() => new Set())
  const [dest, setDest] = useState('')
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState('')
  const move = data.move || { state: 'idle' }
  const running = move.state === 'running'
  const rows = data.models || []
  const chosen = rows.filter(r => r.movable && picked.has(r.path))
  const reloads = chosen.some(r => r.reloads_tier)
  const toggle = (p) => setPicked(s => { const n = new Set(s); if (n.has(p)) n.delete(p); else n.add(p); return n })
  const act = async (fn, after) => {
    setBusy(true); setErr('')
    try { await fn(); if (after) after() } catch (e) { setErr(errText(e)) }
    setBusy(false)
    refresh()
  }

  return (
    <>
      <section className="en-sec">
        <h3 className="en-h">Model files</h3>
        {!rows.length ? <div className="en-note">No model files are configured.</div> : null}
        {rows.map(r => (
          <div key={r.path} className="su-item">
            <div className="su-item-top">
              {r.movable
                ? <input type="checkbox" aria-label={`Move ${r.path}`} checked={picked.has(r.path)}
                         disabled={running || busy} onChange={() => toggle(r.path)} />
                : <span className="su-nobox" aria-hidden="true" />}
              <span className="su-path" title={r.path}>{r.path}</span>
              <span className="su-item-size">{r.exists ? formatBytes(r.size) : 'missing'}</span>
            </div>
            <div className="su-item-meta">
              {r.drive ? <Chip>{r.drive}</Chip> : null}
              {r.removable ? <Chip tone="warn" dot>removable</Chip> : null}
              {r.slow && !r.removable ? <Chip tone="warn" dot>slow drive</Chip> : null}
              {(r.roles || []).map(x => <span key={x} className="su-role-tag" title={x}>{roleWords(x)}</span>)}
            </div>
            {!r.movable && r.why ? <div className="en-reason">{r.why}</div> : null}
          </div>
        ))}
      </section>
      <section className="en-sec">
        <h3 className="en-h">Move to</h3>
        <div className="su-dest">
          <Input aria-label="Move to folder" value={dest} spellCheck={false} disabled={running}
                 placeholder="an existing folder on a fixed drive" onChange={(e) => setDest(e.target.value)} />
          <Button disabled={busy || running || !chosen.length || !dest.trim()}
                  onClick={() => act(() => api.setupMove(chosen.map(r => r.path), dest.trim()), () => setPicked(new Set()))}>
            Move {chosen.length || ''}
          </Button>
        </div>
        {reloads ? <div className="en-reason">Moving these reloads the current tier, so the model servers restart.</div> : null}
        <div className="en-note">Each file is copied and checked; the originals stay where they are.</div>
        {err ? <div className="en-err" role="alert">{err}</div> : null}
        {move.state && move.state !== 'idle' ? (
          <div className="su-moving">
            <div className="su-go">
              <Chip tone={MOVE_TONE[move.state] || 'neutral'} dot busy={running}>{move.state}</Chip>
              {running ? <span className="su-item-size">{formatBytes(move.done_bytes)} of {formatBytes(move.total_bytes)}</span> : null}
              {running ? <Button size="sm" disabled={busy} onClick={() => act(api.setupMoveCancel)}>Cancel move</Button> : null}
            </div>
            {running && move.current ? <div className="en-note">{move.current}</div> : null}
            {running && move.total_bytes
              ? <progress className="su-bar" max={move.total_bytes} value={move.done_bytes || 0} aria-label="Move progress" /> : null}
            {move.error ? <div className="en-err">{move.error}</div> : null}
            <Result res={move.result} />
          </div>
        ) : null}
      </section>
    </>
  )
}

export default function MoveModels() {
  const [ms, setMs] = useState(0)
  const poll = usePoll(api.setupModels, ms)
  const state = poll.data && poll.data.move && poll.data.move.state
  useEffect(() => { setMs(state === 'running' ? 1000 : 0) }, [state])
  return <Body state={poll}>{(data) => <Listing data={data} refresh={poll.refresh} />}</Body>
}
