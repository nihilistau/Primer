import { useEffect, useSyncExternalStore } from 'react'
import * as wm from './windowManager.js'
import { openApp } from './apps.jsx'
import Window from './Window.jsx'
import Dock from './Dock.jsx'
import * as api from './api.js'
import { autoOpen } from './apps/setupPlan.js'
import { markSetupOpened, setupOpened } from './apps/setupSession.js'

const FIRST_RUN_MS = 5000
const FIRST_RUN_TICKS = 120   // about ten minutes: long enough for a slow first model load

/* FIRST RUN: ask /v1/status every few seconds until Setup is needed (no tier works) or the
 * text server is ready. Setup opens once per session; a user who closed it isn't asked again. */
function useFirstRunSetup() {
  useEffect(() => {
    let live = true
    let timer = null
    let ticks = 0
    const tick = async () => {
      let st = null
      try { st = await api.status() } catch { /* Primer still starting: ask again */ }
      if (!live) return
      const next = autoOpen(st, setupOpened())
      if (next === 'open') { markSetupOpened(); openApp(wm, 'setup'); return }
      if (next === 'stop' || ++ticks >= FIRST_RUN_TICKS) return
      timer = setTimeout(tick, FIRST_RUN_MS)
    }
    tick()
    return () => { live = false; clearTimeout(timer) }
  }, [])
}

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
  useFirstRunSetup()

  return (
    <div className="room">
      <main className="desktop">
        {windows.map(w => <Window key={w.id} w={w} focused={w.id === focusedId} />)}
      </main>
      <Dock />
    </div>
  )
}
