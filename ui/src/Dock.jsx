// adapted from shannon-prime-kairos/ui/src/room/Dock.jsx
import { useEffect, useState, useSyncExternalStore } from 'react'
import * as wm from './windowManager.js'
import { APPS, DOCK, openApp } from './apps.jsx'
import { Icon } from './kit/icons.jsx'
import * as api from './api.js'
import { QUIZ_EVENT, READER_RESET_EVENT, dueBadge } from './apps/readerModel.js'

/* THE DOCK: bottom centre. New page, Map of where I've been, Engines. A dot under every app
 * with a window. Minimised windows get a tile of their own after a rule, since a Reader can
 * have many instances and the dock is the only way back to a minimised one. Review wears the
 * count of questions due. */
function Item({ appId, label, icon, open, min, badge, onClick }) {
  return (
    <button className={'dock-item' + (open ? ' dock-open' : '') + (min ? ' dock-min' : '')}
            type="button" data-app={appId} aria-label={label + (badge ? `, ${badge} due` : '') + (open ? ', open' : '')}
            onClick={onClick}>
      <span className="dock-tile"><Icon name={icon} size={22} /></span>
      {badge ? <span className="dock-badge" aria-hidden="true">{badge}</span> : null}
      <span className="dock-tip" aria-hidden="true">{label}</span>
    </button>
  )
}

/* the due count: a cheap read (limit=1) every minute while the tab is visible, when it comes
 * back into view, and after any answer is marked or the reader is reset */
const DUE_EVERY_MS = 60000
function useDueCount() {
  const [n, setN] = useState(0)
  useEffect(() => {
    let live = true
    const read = () => api.quizDue(1).then(r => { if (live) setN((r.counts && r.counts.due) || 0) }).catch(() => {})
    const poll = () => { if (document.visibilityState !== 'hidden') read() }
    const shown = () => { if (document.visibilityState === 'visible') read() }
    read()
    const t = setInterval(poll, DUE_EVERY_MS)
    window.addEventListener(QUIZ_EVENT, read)
    window.addEventListener(READER_RESET_EVENT, read)
    document.addEventListener('visibilitychange', shown)
    return () => {
      live = false; clearInterval(t)
      window.removeEventListener(QUIZ_EVENT, read)
      window.removeEventListener(READER_RESET_EVENT, read)
      document.removeEventListener('visibilitychange', shown)
    }
  }, [])
  return n
}

export default function Dock() {
  const windows = useSyncExternalStore(wm.subscribe, wm.getWindows, wm.getWindows)
  const openIds = new Set(windows.map(w => w.appId))
  const minimised = windows.filter(w => w.minimized)
  const due = dueBadge(useDueCount())
  return (
    <nav className="dock" aria-label="Dock">
      {DOCK.map(id => (
        <Item key={id} appId={id} label={APPS[id].title} icon={APPS[id].icon} badge={id === 'review' ? due : ''}
              open={openIds.has(id)} onClick={() => openApp(wm, id)} />
      ))}
      {minimised.length ? <span className="dock-sep" aria-hidden="true" /> : null}
      {minimised.map(w => (
        <Item key={w.id} appId={w.appId} label={w.title || APPS[w.appId].title} icon={APPS[w.appId].icon}
              min onClick={() => wm.focus(w.id)} />
      ))}
    </nav>
  )
}
