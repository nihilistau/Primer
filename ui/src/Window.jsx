// adapted from shannon-prime-kairos/ui/src/main.jsx (Win, lines 80-235)
import React, { useRef, useState, useEffect, useLayoutEffect } from 'react'
import * as wm from './windowManager.js'
import { APPS } from './apps.jsx'
import { Icon } from './kit/icons.jsx'

const DIRS = ['n', 's', 'e', 'w', 'ne', 'nw', 'se', 'sw']
const MIN_W = 280
const MIN_H = 160

/* One window's body must never take the desktop down. */
class Boundary extends React.Component {
  constructor(p) { super(p); this.state = { err: null } }
  static getDerivedStateFromError(err) { return { err } }
  componentDidCatch(err) { console.error('[window]', this.props.title, err) }
  render() {
    if (!this.state.err) return this.props.children
    return (
      <div style={{ padding: 'var(--sp-4)', color: 'var(--err)' }}>
        This window hit an error; the rest of the desktop is fine.{' '}
        <button onClick={() => this.setState({ err: null })}>retry</button>
        <div style={{ marginTop: 6, fontSize: 11, color: 'var(--text-3)' }}>{String(this.state.err).slice(0, 200)}</div>
      </div>
    )
  }
}

// reduced motion minimises at once; read at the click so the OS setting takes effect live
const minDelay = () => (window.matchMedia && matchMedia('(prefers-reduced-motion: reduce)').matches ? 0 : wm.MIN_MS)

export default function Window({ w, focused }) {
  const app = APPS[w.appId]
  const drag = useRef(null)
  const el = useRef(null)
  // fresh for one beat after mount, so `win-in` plays once; hooks sit above the early return
  const [fresh, setFresh] = useState(true)
  useEffect(() => { const t = setTimeout(() => setFresh(false), 260); return () => clearTimeout(t) }, [])
  const [dragging, setDragging] = useState(false)
  const [restoring, setRestoring] = useState(false)

  // the flight: vector from this window's centre to its dock button, as CSS variables
  const aim = () => {
    const box = el.current
    const btn = document.querySelector('.dock-item[data-app="' + w.appId + '"]')
    if (!box || !btn) return
    const a = box.getBoundingClientRect(), t = btn.getBoundingClientRect()
    box.style.setProperty('--min-dx', Math.round(t.left + t.width / 2 - (a.left + a.width / 2)) + 'px')
    box.style.setProperty('--min-dy', Math.round(t.top + t.height / 2 - (a.top + a.height / 2)) + 'px')
  }
  useLayoutEffect(() => { if (w.minimizing) aim() }, [w.minimizing])
  useLayoutEffect(() => {
    if (!w.restoredAt || !minDelay()) return
    aim()
    setRestoring(true)
    const t = setTimeout(() => setRestoring(false), wm.MIN_MS * 2)
    return () => clearTimeout(t)
  }, [w.restoredAt])

  if (!app || w.minimized) return null
  const Body = app.component

  const onDown = (e) => {
    if (e.target.closest('button')) return
    drag.current = { x: e.clientX, y: e.clientY, ox: w.x, oy: w.y }
    setDragging(true)
    wm.focus(w.id)
    const move = (ev) => {
      if (!drag.current) return
      wm.moveResize(w.id, {
        x: Math.max(0, drag.current.ox + ev.clientX - drag.current.x),
        y: Math.max(0, drag.current.oy + ev.clientY - drag.current.y),
      })
    }
    const up = () => {
      drag.current = null
      setDragging(false)
      window.removeEventListener('mousemove', move)
      window.removeEventListener('mouseup', up)
    }
    window.addEventListener('mousemove', move)
    window.addEventListener('mouseup', up)
  }

  // Eight grips; dragging a top or left edge moves the origin as it resizes.
  const onResize = (dir) => (e) => {
    e.preventDefault(); e.stopPropagation()
    wm.focus(w.id)
    const s = { x: e.clientX, y: e.clientY, ow: w.w, oh: w.h, ox: w.x, oy: w.y }
    const move = (ev) => {
      const dx = ev.clientX - s.x, dy = ev.clientY - s.y
      const p = {}
      if (dir.includes('e')) p.w = Math.max(MIN_W, s.ow + dx)
      if (dir.includes('s')) p.h = Math.max(MIN_H, s.oh + dy)
      if (dir.includes('w')) { p.w = Math.max(MIN_W, s.ow - dx); p.x = s.ox + (s.ow - p.w) }
      if (dir.includes('n')) { p.h = Math.max(MIN_H, s.oh - dy); p.y = s.oy + (s.oh - p.h) }
      wm.moveResize(w.id, p)
    }
    const up = () => {
      window.removeEventListener('mousemove', move)
      window.removeEventListener('mouseup', up)
      document.body.classList.remove('resizing')
    }
    document.body.classList.add('resizing')
    window.addEventListener('mousemove', move)
    window.addEventListener('mouseup', up)
  }

  const title = w.title || app.title
  return (
    <div ref={el}
         className={'win' + (focused ? ' win-focus' : '') + (fresh ? ' win-in' : '')
                    + (dragging ? ' dragging' : '') + (w.max ? ' maxed' : '')
                    + (w.minimizing ? ' win-min-out' : '') + (restoring && !w.minimizing ? ' win-restore' : '')}
         style={w.max ? { zIndex: w.z } : { left: w.x, top: w.y, width: w.w, height: w.h, zIndex: w.z }}
         onMouseDown={() => wm.focus(w.id)}
         onAnimationEnd={(e) => { if (e.target === e.currentTarget && e.animationName === 'win-restore') setRestoring(false) }}
         onAnimationCancel={(e) => { if (e.target === e.currentTarget) setRestoring(false) }}>
      <div className="bar" onMouseDown={onDown}
           onDoubleClick={(e) => { if (!e.target.closest('button')) wm.maximize(w.id) }}>
        <span className="ic"><Icon name={app.icon} size={15} /></span>
        <span className="ti">{title}</span>
        <span className="lights">
          <button className="lt lt-green" onClick={() => wm.maximize(w.id)}
                  title={w.max ? 'Restore' : 'Maximise'} aria-label={w.max ? 'Restore' : 'Maximise'}>
            <svg width="7" height="7" viewBox="0 0 8 8" fill="none" stroke="currentColor" strokeWidth="1.4"><path d="M1.5 1.5h5v5h-5z" /></svg>
          </button>
          <button className="lt lt-amber" onClick={() => wm.minimize(w.id, { delay: minDelay() })} title="Minimise" aria-label="Minimise">
            <svg width="7" height="7" viewBox="0 0 8 8" stroke="currentColor" strokeWidth="1.4"><path d="M1.5 4h5" /></svg>
          </button>
          <button className="lt lt-red" onClick={() => wm.close(w.id)} title="Close" aria-label="Close">
            <svg width="7" height="7" viewBox="0 0 8 8" stroke="currentColor" strokeWidth="1.4"><path d="M2 2l4 4M6 2 2 6" /></svg>
          </button>
        </span>
      </div>
      <div className="body">
        <Boundary title={title}><Body {...w.props} windowId={w.id} /></Boundary>
      </div>
      {w.max ? null : DIRS.map(d => (
        <div key={d} className={'grip g-' + d} onMouseDown={onResize(d)} />
      ))}
    </div>
  )
}
