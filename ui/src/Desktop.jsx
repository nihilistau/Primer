import { useEffect, useSyncExternalStore } from 'react'
import * as wm from './windowManager.js'
import { openApp } from './apps.jsx'
import Window from './Window.jsx'
import Dock from './Dock.jsx'

/* THE DESKTOP: full-viewport backdrop, the windows, and the dock above them. */
export default function Desktop() {
  const windows = useSyncExternalStore(wm.subscribe, wm.getWindows, wm.getWindows)
  // the top window that is showing is the focused one
  const focusedId = windows.filter(w => !w.minimized && !w.minimizing)
    .reduce((top, w) => (!top || w.z > top.z ? w : top), null)?.id

  // On first load, one empty Reader (only when nothing else is open).
  useEffect(() => {
    if (wm.getWindows().length === 0) openApp(wm, 'reader')
  }, [])

  return (
    <div className="room">
      <main className="desktop">
        {windows.map(w => <Window key={w.id} w={w} focused={w.id === focusedId} />)}
      </main>
      <Dock />
    </div>
  )
}
