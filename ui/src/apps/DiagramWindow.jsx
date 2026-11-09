/* DiagramWindow — a drawing of how the page's subject works (or of a phrase on it), by the
 * writing model. It opens the moment a diagram is asked for and shows a status line while the
 * job waits for the model and draws; then the drawing scaled to fit, with every label a button:
 * a click (or Enter) opens a page about that part in the context of the diagrammed page (shift
 * or middle click: a new reader). The caption is below; Again draws afresh; zoom with the buttons
 * or ctrl + wheel.
 *
 * Safety: the server rebuilt the SVG from an allowlist; here it is parsed with DOMParser and
 * rebuilt again (diagram.js), keeping only allowlisted elements and no handler, link or style.
 * It is never inserted as markup. Captions and labels are text.
 * Props: { windowId, diagramId, subject?, nodeId? }. */
import { useEffect, useRef, useState } from 'react'
import * as api from '../api.js'
import * as wm from '../windowManager.js'
import { Button } from '../kit/parts.jsx'
import { openPlace, MEDIA_EVENT } from '../openImage.js'
import { diagramStatusLabel, isEnded, labelHits, pollDiagram, safeSvg, textLabel } from './diagram.js'
import './apps.css'

const ZOOMS = [0.5, 0.75, 1, 1.5, 2, 3]

export default function DiagramWindow({ windowId, diagramId, subject = '', nodeId = '' }) {
  const [curId, setCurId] = useState(diagramId)
  const [nonce, setNonce] = useState(0)          // bumps on Again, so the poll restarts even on the same id
  const [job, setJob] = useState(null)
  const [pollErr, setPollErr] = useState('')
  const [gone, setGone] = useState(false)
  const [drawErr, setDrawErr] = useState('')
  const [zi, setZi] = useState(ZOOMS.indexOf(1))
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState('')
  const box = useRef(null)
  const scroller = useRef(null)
  const openRef = useRef(null)

  // poll this diagram to its end
  useEffect(() => {
    const ac = new AbortController()
    setGone(false)
    pollDiagram(curId, (d) => { setJob(d); setPollErr('') }, { signal: ac.signal, onError: (e) => {
      if (e.status === 404) setGone(true)
      else setPollErr(e.message || 'Could not reach Primer.')
    } })
    return () => ac.abort()
  }, [curId, nonce])

  // a label opens its page in the context of the diagrammed page
  openRef.current = (label, newWindow) => {
    if (!label) return
    const from = (job && job.node_id) || nodeId
    openPlace(from ? { concept: label, via: 'context', from_id: from, phrase: label }
                   : { concept: label, via: 'typed' }, { newWindow })
  }

  // the drawing: parsed, rebuilt from the allowlist, its labels made buttons
  const svgText = job && job.status === 'done' ? job.svg : ''
  useEffect(() => {
    const host = box.current
    if (!host) return undefined
    host.replaceChildren()
    setDrawErr('')
    if (!svgText) return undefined
    let svg = null
    try { svg = safeSvg(svgText) } catch { svg = null }
    if (!svg) { setDrawErr('This drawing could not be shown.'); return undefined }
    svg.removeAttribute('width'); svg.removeAttribute('height')
    svg.setAttribute('width', '100%'); svg.setAttribute('height', '100%')
    svg.setAttribute('preserveAspectRatio', 'xMidYMid meet')
    svg.setAttribute('class', 'dg-svg')
    svg.setAttribute('role', 'group')
    svg.setAttribute('aria-label', 'Diagram')
    const off = []
    svg.querySelectorAll('text').forEach((t) => {
      const label = textLabel(t)
      if (!label) return
      t.setAttribute('class', 'dg-label')
      t.setAttribute('role', 'button')
      t.setAttribute('tabindex', '0')
      t.setAttribute('aria-label', `${label}: open a page about it`)
      const click = (e) => { e.preventDefault(); openRef.current(label, e.shiftKey || e.ctrlKey || e.metaKey) }
      const aux = (e) => { if (e.button === 1) { e.preventDefault(); openRef.current(label, true) } }
      const key = (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); openRef.current(label, e.shiftKey || e.ctrlKey || e.metaKey) } }
      t.addEventListener('click', click); t.addEventListener('auxclick', aux); t.addEventListener('keydown', key)
      off.push(() => { t.removeEventListener('click', click); t.removeEventListener('auxclick', aux); t.removeEventListener('keydown', key) })
    })
    host.appendChild(svg)
    return () => { off.forEach(f => f()); host.replaceChildren() }
  }, [svgText])

  // ctrl + wheel zooms (a plain wheel scrolls a zoomed drawing)
  useEffect(() => {
    const el = scroller.current
    if (!el) return undefined
    const onWheel = (e) => {
      if (!e.ctrlKey) return
      e.preventDefault()
      setZi(i => Math.max(0, Math.min(ZOOMS.length - 1, i + (e.deltaY < 0 ? 1 : -1))))
    }
    el.addEventListener('wheel', onWheel, { passive: false })
    return () => el.removeEventListener('wheel', onWheel)
  }, [])

  const phrase = job && job.phrase
  useEffect(() => {
    const t = subject || phrase || ''
    wm.setTitle(windowId, t ? `Diagram: ${t}` : 'Diagram')
  }, [windowId, subject, phrase])

  const again = async () => {
    setBusy(true); setErr('')
    try {
      const r = await api.diagramAgain(curId)
      setJob({ status: 'queued', node_id: job && job.node_id, phrase: job && job.phrase, labels: [] })
      setCurId(r.id || curId); setNonce(n => n + 1)
      window.dispatchEvent(new Event(MEDIA_EVENT))
    } catch (e) { setErr(e.message || 'Could not start the diagram again.') }
    setBusy(false)
  }

  const ended = isEnded(job)
  const failed = job && job.status === 'failed'
  const label = gone ? 'This diagram no longer exists.' : diagramStatusLabel(job)
  const parts = job && job.status === 'done' ? labelHits(job.labels) : []
  const zoom = ZOOMS[zi]
  return (
    <div className="dg">
      <div className="dg-top">
        <div className={'dg-status' + (failed || gone ? ' dg-status-err' : '')} role="status">
          {ended || gone ? null : <span className="ui-spin" aria-hidden="true" />} {label}
        </div>
        <div className="dg-tools">
          <Button size="sm" variant="ghost" disabled={!svgText || zi >= ZOOMS.length - 1} onClick={() => setZi(i => i + 1)}
                  aria-label="Zoom in" title="Zoom in">+</Button>
          <Button size="sm" variant="ghost" disabled={!svgText || zi <= 0} onClick={() => setZi(i => i - 1)}
                  aria-label="Zoom out" title="Zoom out">−</Button>
          <Button size="sm" variant="ghost" disabled={!svgText || zoom === 1} onClick={() => setZi(ZOOMS.indexOf(1))}
                  title="Fit the drawing">Fit</Button>
          <Button size="sm" disabled={busy || !ended} onClick={again}>Again</Button>
        </div>
      </div>
      <div className="dg-stage" ref={scroller}>
        <div className="dg-paper" style={{ width: `${zoom * 100}%`, height: `${zoom * 100}%` }} ref={box} />
        {drawErr ? <div className="dg-note dg-note-err">{drawErr}</div> : null}
        {!job || (!ended && !gone) ? <div className="dg-note">{label}</div> : null}
      </div>
      {job && job.status === 'done' && job.caption ? <p className="dg-caption">{job.caption}</p> : null}
      {parts.length ? (
        <ul className="dg-parts" aria-label="Parts">
          {parts.map(p => (
            <li key={p.label}>
              <button type="button" className="dg-part" title={`Read about ${p.label}`}
                      onClick={(e) => openRef.current(p.label, e.shiftKey || e.ctrlKey || e.metaKey)}
                      onAuxClick={(e) => { if (e.button === 1) { e.preventDefault(); openRef.current(p.label, true) } }}>
                {p.label}
              </button>
            </li>
          ))}
        </ul>
      ) : null}
      {pollErr ? <div className="dg-err">{pollErr}</div> : null}
      {err ? <div className="dg-err">{err}</div> : null}
    </div>
  )
}
