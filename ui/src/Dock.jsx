// adapted from shannon-prime-kairos/ui/src/room/Dock.jsx
import { useSyncExternalStore } from 'react'
import * as wm from './windowManager.js'
import { APPS, DOCK, openApp } from './apps.jsx'
import { Icon } from './kit/icons.jsx'

/* THE DOCK: bottom centre. New page, Map of where I've been, Engines. A dot under every app
 * with a window. Minimised windows get a tile of their own after a rule, since a Reader can
 * have many instances and the dock is the only way back to a minimised one. */
function Item({ appId, label, icon, open, min, onClick }) {
  return (
    <button className={'dock-item' + (open ? ' dock-open' : '') + (min ? ' dock-min' : '')}
            type="button" data-app={appId} aria-label={label + (open ? ', open' : '')}
            onClick={onClick}>
      <span className="dock-tile"><Icon name={icon} size={22} /></span>
      <span className="dock-tip" aria-hidden="true">{label}</span>
    </button>
  )
}

export default function Dock() {
  const windows = useSyncExternalStore(wm.subscribe, wm.getWindows, wm.getWindows)
  const openIds = new Set(windows.map(w => w.appId))
  const minimised = windows.filter(w => w.minimized)
  return (
    <nav className="dock" aria-label="Dock">
      {DOCK.map(id => (
        <Item key={id} appId={id} label={APPS[id].title} icon={APPS[id].icon}
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
