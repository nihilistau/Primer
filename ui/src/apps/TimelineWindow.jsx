/* TimelineWindow — the dated events a page names, on a horizontal axis fitted to their span. It
 * opens the moment a timeline is asked for and shows a status line while the fast model reads
 * the dates, then pins in rows whose labels never overlap; a date known only roughly (c., a
 * decade, a century) is a faint span, a range a bar. Ctrl + wheel or the buttons zoom, a drag pans. A
 * click on an event opens a page about it in the context of the page it came from (shift or
 * middle click: a new reader). All my timelines puts every finished timeline on one axis,
 * coloured by page. Labels are model text: rendered as text only.
 * Props: { windowId, timelineId?, subject?, nodeId?, all? }. */
import { useEffect, useMemo, useRef, useState } from 'react'
import * as api from '../api.js'
import * as wm from '../windowManager.js'
import { Button } from '../kit/parts.jsx'
import { openPlace, MEDIA_EVENT } from '../openImage.js'
import { axis, formatDate, isEnded, layout, legend, pageColours, pan, pollTimeline, timelineStatusLabel,
         yearAt, zoom } from './timeline.js'
import './apps.css'

const TOP = 30          // the axis and its labels
const ROW_H = 26
const NONE = []         // one empty list, so the memos below see the same array until the events change

export default function TimelineWindow({ windowId, timelineId = '', subject = '', nodeId = '', all = false }) {
  const [curId, setCurId] = useState(timelineId)
  const [nonce, setNonce] = useState(0)          // bumps on Again, so the poll restarts even on the same id
  const [job, setJob] = useState(null)
  const [pollErr, setPollErr] = useState('')
  const [gone, setGone] = useState(false)
  const [showAll, setShowAll] = useState(!!all || !timelineId)
  const [allState, setAllState] = useState({ loading: false, events: null, error: '' })
  const [width, setWidth] = useState(0)
  const [range, setRange] = useState(null)       // {min, max} once zoomed or panned; null fits the events
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState('')
  const stage = useRef(null)
  const drag = useRef(null)

  // poll this timeline to its end
  useEffect(() => {
    if (!curId) return undefined
    const ac = new AbortController()
    setGone(false)
    pollTimeline(curId, (t) => { setJob(t); setPollErr('') }, { signal: ac.signal, onError: (e) => {
      if (e.status === 404) setGone(true)
      else setPollErr(e.message || 'Could not reach Primer.')
    } })
    return () => ac.abort()
  }, [curId, nonce])

  // All my timelines, read afresh each time it is shown
  useEffect(() => {
    if (!showAll) return undefined
    let live = true
    setAllState(s => ({ ...s, loading: true, error: '' }))
    api.allTimelines()
      .then(r => { if (live) setAllState({ loading: false, events: Array.isArray(r.events) ? r.events : [], error: '' }) })
      .catch(e => { if (live) setAllState({ loading: false, events: null, error: e.message || 'Could not load your timelines.' }) })
    return () => { live = false }
  }, [showAll])

  // the stage's width: the axis is fitted to it
  useEffect(() => {
    const el = stage.current
    if (!el) return undefined
    const measure = () => setWidth(Math.max(0, Math.floor(el.clientWidth)))
    measure()
    if (typeof ResizeObserver === 'undefined') return undefined
    const ro = new ResizeObserver(measure)
    ro.observe(el)
    return () => ro.disconnect()
  }, [])

  // the memos key on the events array itself: a poll that brings a new list (Again, a new
  // All my timelines read) refits, the same list does not
  const events = showAll ? (allState.events || NONE) : ((job && job.status === 'done' && job.events) || NONE)
  useEffect(() => { setRange(null) }, [events])   // new events: fit them again

  const fitted = useMemo(() => (width ? axis(events, width) : null), [events, width])
  const ax = fitted && range ? zoom({ ...range, width }, 1) : fitted
  const rows = useMemo(() => layout(events, ax), [events, ax && ax.min, ax && ax.max, ax && ax.width])  // eslint-disable-line react-hooks/exhaustive-deps
  const colours = useMemo(() => (showAll ? pageColours(events) : null), [showAll, events])
  const key = useMemo(() => (showAll ? legend(events) : []), [showAll, events])

  const zoomBy = (factor, at) => { if (ax) { const z = zoom(ax, factor, at); setRange({ min: z.min, max: z.max }) } }
  const panBy = (years) => { if (ax) { const p = pan(ax, years); setRange({ min: p.min, max: p.max }) } }

  // ctrl/cmd + wheel zooms about the year under the pointer (a native listener: React's is
  // passive); a plain wheel scrolls the rows as usual
  const axRef = useRef(ax)
  axRef.current = ax
  useEffect(() => {
    const el = stage.current
    if (!el) return undefined
    const onWheel = (e) => {
      const a = axRef.current
      if (!a || !(e.ctrlKey || e.metaKey)) return
      e.preventDefault()
      const x = e.clientX - el.getBoundingClientRect().left
      const z = zoom(a, e.deltaY < 0 ? 1.25 : 0.8, yearAt(a, x))
      setRange({ min: z.min, max: z.max })
    }
    el.addEventListener('wheel', onWheel, { passive: false })
    return () => el.removeEventListener('wheel', onWheel)
  }, [])

  // a drag pans; a drag that moved is not also a click on the pin it started on
  const onPointerDown = (e) => {
    if (!ax || e.button !== 0) return
    drag.current = { x: e.clientX, min: ax.min, max: ax.max, moved: false, id: e.pointerId }
  }
  const onPointerMove = (e) => {
    const d = drag.current
    if (!d || !ax) return
    const dx = e.clientX - d.x
    if (!d.moved && Math.abs(dx) < 4) return
    if (!d.moved) { d.moved = true; try { e.currentTarget.setPointerCapture(d.id) } catch { /* fine */ } }
    const years = -dx * ((d.max - d.min) / ax.width)
    const p = pan({ ...ax, min: d.min, max: d.max }, years)
    setRange({ min: p.min, max: p.max })
  }
  const onPointerUp = () => {
    const d = drag.current
    drag.current = null
    if (d && d.moved) { const until = Date.now() + 50; stage.current.dataset.noclick = String(until) }
  }
  const dragged = () => Number((stage.current && stage.current.dataset.noclick) || 0) > Date.now()

  const onStageKey = (e) => {
    if (!ax || e.target !== e.currentTarget) return
    const span = ax.max - ax.min
    if (e.key === 'ArrowLeft') { e.preventDefault(); panBy(-span * 0.1) }
    else if (e.key === 'ArrowRight') { e.preventDefault(); panBy(span * 0.1) }
    else if (e.key === '+' || e.key === '=') { e.preventDefault(); zoomBy(1.5) }
    else if (e.key === '-') { e.preventDefault(); zoomBy(1 / 1.5) }
    else if (e.key === '0') { e.preventDefault(); setRange(null) }
  }

  // an event opens its page in the context of the page it came from
  const openEvent = (ev, newWindow) => {
    if (dragged()) return
    const from = ev.node_id || (job && job.node_id) || nodeId
    const label = ev.label || ''
    if (!label) return
    openPlace(from ? { concept: label, via: 'context', from_id: from, phrase: label }
                   : { concept: label, via: 'typed' }, { newWindow })
  }

  const pageTitle = job && job.page_title
  useEffect(() => {
    const t = subject || pageTitle || ''
    wm.setTitle(windowId, showAll ? 'All my timelines' : t ? `Timeline: ${t}` : 'Timeline')
  }, [windowId, subject, pageTitle, showAll])

  const again = async () => {
    setBusy(true); setErr('')
    try {
      const r = await api.timelineAgain(curId)
      setJob({ status: 'reading', events: [], node_id: job && job.node_id, page_title: job && job.page_title })
      setCurId(r.id || curId); setNonce(n => n + 1)
      window.dispatchEvent(new Event(MEDIA_EVENT))
    } catch (e) { setErr(e.message || 'Could not start the timeline again.') }
    setBusy(false)
  }

  const ended = isEnded(job)
  const failed = !showAll && job && job.status === 'failed'
  let label
  if (showAll) {
    const pages = key.length
    label = allState.loading ? 'Loading your timelines…'
      : allState.error ? allState.error
      : events.length ? `${events.length} ${events.length === 1 ? 'event' : 'events'} from ${pages} ${pages === 1 ? 'page' : 'pages'}`
      : 'No finished timelines yet'
  } else label = gone ? 'This timeline no longer exists.' : timelineStatusLabel(job)
  const spinning = showAll ? allState.loading : !(ended || gone)
  const empty = showAll ? !allState.loading && !allState.error && !events.length
                        : job && job.status === 'done' && !events.length
  const height = TOP + Math.max(1, rows.length) * ROW_H + 12

  return (
    <div className="tmw">
      <div className="tmw-top">
        <div className={'tmw-label' + (failed || gone || (showAll && allState.error) ? ' tmw-label-err' : '')} role="status">
          {spinning ? <span className="ui-spin" aria-hidden="true" /> : null} {label}
        </div>
        <div className="tmw-tools">
          <Button size="sm" variant="ghost" disabled={!ax} onClick={() => zoomBy(1.5)} aria-label="Zoom in" title="Zoom in">+</Button>
          <Button size="sm" variant="ghost" disabled={!ax} onClick={() => zoomBy(1 / 1.5)} aria-label="Zoom out" title="Zoom out">−</Button>
          <Button size="sm" variant="ghost" disabled={!ax || !range} onClick={() => setRange(null)} title="Fit every event">Fit</Button>
          <Button size="sm" aria-pressed={showAll} disabled={showAll && !curId}
                  title={showAll ? (curId ? 'Back to this page’s timeline' : 'Every finished timeline') : 'Every finished timeline on one axis'}
                  onClick={() => setShowAll(v => !v)}>All my timelines</Button>
          {showAll ? null : <Button size="sm" disabled={busy || !ended || !curId} onClick={again}>Again</Button>}
        </div>
      </div>
      <div className="tmw-stage" ref={stage} tabIndex={0} role="region"
           aria-label="Timeline: arrow keys pan, plus and minus zoom (or ctrl + wheel), 0 fits"
           onKeyDown={onStageKey} onPointerDown={onPointerDown} onPointerMove={onPointerMove}
           onPointerUp={onPointerUp} onPointerCancel={onPointerUp}>
        {ax ? (
          <svg className="tmw-svg" width={ax.width} height={height} role="presentation">
            {ax.ticks.map(t => (
              <g key={t.year} className="tmw-tick">
                <line x1={t.x} x2={t.x} y1={TOP - 6} y2={height} />
                <text x={t.x} y={TOP - 12} textAnchor="middle">{t.label}</text>
              </g>
            ))}
            <line className="tmw-axis" x1={0} x2={ax.width} y1={TOP - 6} y2={TOP - 6} />
            {rows.map((row, r) => row.map(p => {
              const y = TOP + r * ROW_H + ROW_H / 2
              const ev = p.event
              const date = formatDate(ev)
              const c = colours ? ` tmw-c${colours.get(ev.node_id) || 0}` : ''
              const x0 = p.span ? Math.max(0, p.span.x0) : 0, x1 = p.span ? Math.min(ax.width, p.span.x1) : 0
              const where = showAll && ev.page_title ? ` · ${ev.page_title}` : ''
              return (
                <g key={p.index} className={'tmw-ev' + c} role="button" tabIndex={0}
                   aria-label={`${ev.label}, ${date}${where}. Open a page about it`}
                   onClick={(e) => openEvent(ev, e.shiftKey || e.ctrlKey || e.metaKey)}
                   onAuxClick={(e) => { if (e.button === 1) { e.preventDefault(); openEvent(ev, true) } }}
                   onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); openEvent(ev, e.shiftKey || e.ctrlKey || e.metaKey) } }}>
                  <title>{`${ev.label} (${date})${where}`}</title>
                  {p.span ? <rect className={p.span.kind === 'range' ? 'tmw-range' : 'tmw-approx'}
                                  x={x0} y={y - (p.span.kind === 'range' ? 3 : 6)} width={Math.max(1, x1 - x0)}
                                  height={p.span.kind === 'range' ? 6 : 12} rx={3} /> : null}
                  <circle className="tmw-pin" cx={p.x} cy={y} r={4.5} />
                  <text className="tmw-ev-label" x={p.flip ? p.lx - 9 : p.lx + 9} y={y} dy="0.35em"
                        textAnchor={p.flip ? 'end' : 'start'}>{p.text}</text>
                </g>
              )
            }))}
          </svg>
        ) : null}
        {empty ? <div className="tmw-note">{showAll ? 'No finished timelines yet.' : 'No dated events on this page'}</div> : null}
      </div>
      {showAll && key.length ? (
        <ul className="tmw-legend" aria-label="Pages">
          {key.map(k => (
            <li key={k.node_id} className={`tmw-c${k.colour}`}><span className="tmw-swatch" aria-hidden="true" />{k.title}</li>
          ))}
        </ul>
      ) : null}
      {pollErr && !showAll ? <div className="tmw-err">{pollErr}</div> : null}
      {err ? <div className="tmw-err">{err}</div> : null}
    </div>
  )
}
