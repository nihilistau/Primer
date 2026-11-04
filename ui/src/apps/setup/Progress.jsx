/* the run: its overall state, Pause/Resume, and per item the state, bytes, speed, time left
 * and Cancel */
import { Button, Chip } from '../../kit/parts.jsx'
import { formatBytes, formatEta, formatSpeed, itemState } from '../setupPlan.js'

const OVERALL = {
  running: { tone: 'accent', busy: true, label: 'downloading' },
  paused: { tone: 'warn', label: 'paused' },
  done: { tone: 'accent', busy: true, label: 'writing the tier' },
  failed: { tone: 'err', label: 'stopped with errors' },
  ready: { tone: 'ok', label: 'ready' },
}
const CANCELLABLE = ['queued', 'downloading', 'verifying', 'extracting', 'failed']

function bytesLine(r) {
  if (!r.total) return r.bytes ? formatBytes(r.bytes) : ''
  return `${formatBytes(r.bytes || 0)} of ${formatBytes(r.total)}`
}

export default function Progress({ st, busy, onPause, onResume, onCancel }) {
  const o = OVERALL[st.state] || { tone: 'neutral', label: st.state }
  return (
    <>
      <div className="su-go">
        <Chip tone={o.tone} dot busy={o.busy}>{o.label}</Chip>
        {st.state === 'running' ? <Button size="sm" disabled={busy} onClick={onPause}>Pause</Button> : null}
        {st.state === 'paused' || st.state === 'failed'
          ? <Button size="sm" disabled={busy} onClick={onResume}>Resume</Button> : null}
      </div>
      {st.error ? <div className="en-err" role="alert">{st.error}</div> : null}
      <div className="su-prog">
        {(st.items || []).map(r => {
          const s = itemState(r)
          return (
            <div key={r.id} className="su-item">
              <div className="su-item-top">
                <span className="su-item-name">{r.label || r.id}</span>
                <Chip tone={s.tone} dot busy={s.busy}>{s.label}</Chip>
                <span className="su-item-size">
                  {[bytesLine(r), formatSpeed(r.speed_bps), formatEta(r.eta_s)].filter(Boolean).join(' · ')}
                </span>
                {CANCELLABLE.includes(r.state)
                  ? <Button size="sm" variant="ghost" disabled={busy} onClick={() => onCancel(r.id)}
                            aria-label={`Cancel ${r.label || r.id}`}>Cancel</Button> : null}
              </div>
              {s.progress !== null && r.state !== 'present' && r.state !== 'done'
                ? <progress className="su-bar" max={1} value={s.progress} aria-label={`${r.label || r.id} progress`} /> : null}
              {r.error ? <div className="en-reason">{r.error}</div> : null}
            </div>
          )
        })}
      </div>
    </>
  )
}
