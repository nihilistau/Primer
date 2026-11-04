/* Check for updates: newer ZIMs and runtime builds, listed only; nothing is updated. A
 * catalogue that failed shows its reason quietly on its row. */
import { useState } from 'react'
import * as api from '../../api.js'
import { Button } from '../../kit/parts.jsx'
import { errText, formatBytes } from '../setupPlan.js'

export default function Updates() {
  const [rows, setRows] = useState(null)
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState('')
  const check = async () => {
    setBusy(true); setErr('')
    try { setRows(await api.setupUpdates(rows !== null)) } catch (e) { setErr(errText(e)) }
    setBusy(false)
  }
  const newer = (rows || []).filter(r => !r.error)
  const failed = (rows || []).filter(r => r.error)
  return (
    <section className="en-sec">
      <h3 className="en-h">Updates</h3>
      <div className="su-go">
        <Button disabled={busy} onClick={check}>{busy ? 'Checking…' : rows === null ? 'Check for updates' : 'Check again'}</Button>
      </div>
      {err ? <div className="en-err" role="alert">{err}</div> : null}
      {rows !== null && !newer.length && !err
        ? <div className="en-note">{failed.length ? 'Nothing newer among the ones that could be checked.' : 'Everything is up to date.'}</div> : null}
      {newer.map(r => (
        <div key={r.item} className="su-item">
          <div className="su-item-top">
            <span className="su-item-name">{r.label || r.item}</span>
            <span className="su-item-size">{r.installed} → {r.latest}{r.size ? ` · ${formatBytes(r.size)}` : ''}</span>
          </div>
        </div>
      ))}
      {failed.map(r => (
        <div key={r.item} className="en-note">{r.label || r.item}: couldn&rsquo;t check ({r.error})</div>
      ))}
      {rows !== null ? <div className="en-note">Nothing is updated automatically.</div> : null}
    </section>
  )
}
