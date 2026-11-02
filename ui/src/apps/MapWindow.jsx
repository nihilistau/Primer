/* MapWindow — the places a page names, pinned on the offline map. It opens the moment a map is
 * asked for, shows a status line while Primer finds and locates the places, then the map fitted
 * to the pins beside (or above) the place list. A click on a pin or a listed place opens that
 * place's page as a General branch from the mapped page; shift or middle click opens a new
 * reader. Again looks the places up anew. MapLibre loads on mount, in its own chunk.
 * Props: { windowId, mapId, subject?, nodeId? }. */
import { useEffect, useRef, useState } from 'react'
import * as api from '../api.js'
import * as wm from '../windowManager.js'
import { Button } from '../kit/parts.jsx'
import { Icon } from '../kit/icons.jsx'
import { openPlace, MEDIA_EVENT } from '../openImage.js'
import { fitBounds, isEnded, located, mapStatusLabel, placeConcept, pollMap } from './mapJob.js'
import './apps.css'

export default function MapWindow({ windowId, mapId, subject = '', nodeId = '' }) {
  const [curId, setCurId] = useState(mapId)
  const [nonce, setNonce] = useState(0)          // bumps on Again, so the poll restarts even on the same id
  const [tileErrs, setTileErrs] = useState(0)
  const [job, setJob] = useState(null)
  const [pollErr, setPollErr] = useState('')
  const [gone, setGone] = useState(false)
  const [lib, setLib] = useState(null)           // {gl: the maplibre-gl module, buildStyle} once loaded
  const [loadErr, setLoadErr] = useState('')
  const [mapErr, setMapErr] = useState('')
  const [mapUp, setMapUp] = useState(false)    // the MapLibre map exists (it needs the tilejson first)
  const [selected, setSelected] = useState(-1)
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState('')
  const box = useRef(null)
  const mapRef = useRef(null)
  const markers = useRef([])
  const openRef = useRef(null)

  // poll this map to its end
  useEffect(() => {
    const ac = new AbortController()
    setGone(false)
    pollMap(curId, (j) => { setJob(j); setPollErr('') }, { signal: ac.signal, onError: (e) => {
      if (e.status === 404) setGone(true)
      else setPollErr(e.message || 'Could not reach Primer.')
    } })
    return () => ac.abort()
  }, [curId, nonce])

  // MapLibre and its stylesheet, in a chunk of their own
  useEffect(() => {
    let live = true
    // the style builder carries @protomaps/basemaps, so it loads here too, not in the main bundle
    // MapLibre 6 looks for its worker beside its own module, which the bundle doesn't have: Vite
    // emits the (self-contained) worker as an asset and MapLibre is told where it is
    Promise.all([import('maplibre-gl'), import('maplibre-gl/dist/maplibre-gl.css'), import('./mapStyle.js'),
                 import('maplibre-gl/dist/maplibre-gl-worker.mjs?url')])
      .then(([m, , st, worker]) => {
        const gl = m.default || m
        gl.setWorkerUrl(new URL(worker.default, window.location.origin).href)
        if (live) setLib({ gl, buildStyle: st.buildStyle })
      })
      .catch((e) => { if (live) setLoadErr(e.message || 'The map library could not be loaded.') })
    return () => { live = false }
  }, [])

  // the map itself, made once the library is here (and the tilejson, for the zoom range)
  useEffect(() => {
    if (!lib || !box.current) return undefined
    let live = true, map = null
    api.mapTileJson().catch(() => null).then((tj) => {
      if (!live) return
      try {
        map = new lib.gl.Map({
          container: box.current, style: lib.buildStyle(window.location.origin, tj),
          center: [0, 20], zoom: 1, attributionControl: { compact: false }, dragRotate: false,
        })
      } catch (e) { setMapErr(e.message || 'This browser cannot draw the map.'); return }
      map.touchZoomRotate.disableRotation()
      map.addControl(new lib.gl.NavigationControl({ showCompass: false }), 'top-right')
      map.on('error', (e) => {
        if (!map.isStyleLoaded()) setMapErr((e.error && e.error.message) || 'The map could not be drawn.')
        else if (e.sourceId || (e.tile) || /glyph|sprite|tile/i.test((e.error && e.error.message) || '')) setTileErrs(n => n + 1)
      })
      map.on('load', () => setMapErr(''))
      mapRef.current = map
      setMapUp(true)      // the pins go on now that the map exists
    })
    return () => {
      live = false
      markers.current.forEach(m => m.remove()); markers.current = []
      mapRef.current = null; setMapUp(false)
      if (map) map.remove()
    }
  }, [lib])

  const places = (job && job.places) || []
  const pinKey = JSON.stringify(places.map(p => [p.name, p.lat, p.lon]))

  // open a place: a General branch from the mapped page
  openRef.current = (p, newWindow) => {
    const from = (job && job.node_id) || nodeId
    openPlace({ concept: placeConcept(p), via: 'general', ...(from ? { from_id: from } : {}), phrase: p.name }, { newWindow })
  }

  // a pin per located place, and the view fitted to them
  useEffect(() => {
    const map = mapRef.current
    if (!lib || !map) return
    markers.current.forEach(m => m.remove()); markers.current = []
    places.forEach((p, i) => {
      if (!located(p)) return
      const el = document.createElement('button')
      el.type = 'button'; el.className = 'mp-pin'; el.dataset.idx = String(i)
      el.setAttribute('aria-label', `${p.name}${p.what ? ', ' + p.what : ''}`)
      const lab = document.createElement('span'); lab.className = 'mp-pin-label'; lab.textContent = p.name
      el.append(lab)
      el.addEventListener('click', (e) => { e.stopPropagation(); setSelected(i); openRef.current(p, e.shiftKey || e.ctrlKey || e.metaKey) })
      el.addEventListener('auxclick', (e) => { if (e.button === 1) { e.preventDefault(); setSelected(i); openRef.current(p, true) } })
      el.addEventListener('mouseenter', () => setSelected(i))
      markers.current.push(new lib.gl.Marker({ element: el }).setLngLat([p.lon, p.lat]).addTo(map))
    })
    const b = fitBounds(places)
    if (b) map.fitBounds(b, { padding: 56, maxZoom: 8, duration: 0 })
  }, [lib, mapUp, pinKey])      // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    markers.current.forEach(m => {
      const el = m.getElement()
      el.classList.toggle('mp-pin-on', Number(el.dataset.idx) === selected)
    })
  }, [selected, pinKey, lib, mapUp])

  const phrase = job && job.phrase
  useEffect(() => {
    const t = subject || phrase || ''
    wm.setTitle(windowId, t ? `Map: ${t}` : 'Map')
  }, [windowId, subject, phrase])

  const again = async () => {
    setBusy(true); setErr('')
    try {
      const r = await api.mapAgain(curId)
      setSelected(-1); setJob({ status: 'finding', places: [], node_id: job && job.node_id, phrase: job && job.phrase })
      setCurId(r.id || curId); setNonce(n => n + 1); setTileErrs(0)
      window.dispatchEvent(new Event(MEDIA_EVENT))
    } catch (e) { setErr(e.message || 'Could not start the map again.') }
    setBusy(false)
  }

  const ended = isEnded(job)
  const label = gone ? 'This map no longer exists.' : mapStatusLabel(job)
  const failed = job && job.status === 'failed'
  const mapped = places.filter(located).length
  return (
    <div className="mp">
      <div className="mp-top">
        <div className={'mp-label' + (failed || gone ? ' mp-label-err' : '')} role="status">
          {ended || gone ? null : <span className="ui-spin" aria-hidden="true" />} {label}
        </div>
        <Button size="sm" disabled={busy || !ended} onClick={again}>Again</Button>
      </div>
      <div className="mp-main">
        <div className="mp-stage">
          <div className="mp-canvas" ref={box} role="region" aria-label="Map" />
          {!lib && !loadErr ? <div className="mp-note">Loading the map…</div> : null}
          {loadErr ? <div className="mp-note mp-note-err">The map could not be loaded: {loadErr}</div> : null}
          {mapErr && !loadErr ? <div className="mp-note mp-note-err">{mapErr}</div> : null}
          {tileErrs && !mapErr ? <div className="mp-note">Some map tiles could not be loaded.</div> : null}
          {ended && !failed && !mapped ? <div className="mp-note">No place could be pinned.</div> : null}
        </div>
        <ul className="mp-list" aria-label="Places">
          {places.map((p, i) => located(p) ? (
            <li key={i}>
              <button type="button" className={'mp-item' + (i === selected ? ' mp-item-on' : '')}
                      title={`Read about ${placeConcept(p)}`}
                      onMouseEnter={() => setSelected(i)} onFocus={() => setSelected(i)}
                      onClick={(e) => { setSelected(i); openRef.current(p, e.shiftKey || e.ctrlKey || e.metaKey) }}
                      onAuxClick={(e) => { if (e.button === 1) { e.preventDefault(); openRef.current(p, true) } }}>
                <Icon name="map" size={14} />
                <span className="mp-item-text">
                  <b>{p.name}</b>
                  {p.what ? <span className="mp-what">{p.what}</span> : null}
                </span>
              </button>
            </li>
          ) : (
            <li key={i} className="mp-item mp-item-off">
              <span className="mp-item-text">
                <b>{p.name}</b>
                <span className="mp-what">{p.what ? `${p.what} · ` : ''}not found</span>
              </span>
            </li>
          ))}
        </ul>
      </div>
      {pollErr ? <div className="mp-err">{pollErr}</div> : null}
      {err ? <div className="mp-err">{err}</div> : null}
    </div>
  )
}
