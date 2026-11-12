import { useEffect, useState, useSyncExternalStore } from 'react'
import * as wm from './windowManager.js'
import { openApp } from './apps.jsx'
import Window from './Window.jsx'
import Dock from './Dock.jsx'
import * as api from './api.js'
import { autoOpen } from './apps/setupPlan.js'
import { markSetupOpened, setupOpened } from './apps/setupSession.js'
import { NOTE_MS, createListener, dispatchCommand, kairosNote, watchFocus, watchRemoteEnabled } from './remote.js'
import KairosNote from './KairosNote.jsx'

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

/* KAIROS (Phase 15): while "Let Kairos drive Primer" is on (read from /v1/status, and at once
 * when this tab's Engines window flips it: remote.js watchRemoteEnabled), this tab listens for
 * Kairos's commands, hands each to the most recently focused Reader (or opens one), and reports
 * that Reader's page to the server when it changes (while the tab is visible) and whenever the
 * tab gains focus, so "that" means the page in front (remote.js watchFocus). With the switch off
 * it holds no stream open. Its note, "Kairos asked: …", is the desktop's, above every window,
 * for NOTE_MS. */
function useKairos() {
  const [note, setNote] = useState(null)   // {text, key} or null
  useEffect(() => {
    const lis = createListener({
      onCommand: (cmd, finish) => {
        setNote(kairosNote(cmd))
        dispatchCommand(cmd, finish, { windows: wm.getWindows(), openReader: () => openApp(wm, 'reader') })
      },
      enabled: false,   // until the status says the switch is on
    })
    const unswitch = watchRemoteEnabled({ listener: lis })
    const unwatch = watchFocus({ listener: lis, getWindows: wm.getWindows, subscribeWindows: wm.subscribe })
    return () => { unswitch(); unwatch(); lis.close() }
  }, [])
  useEffect(() => {
    if (!note) return
    const t = setTimeout(() => setNote(null), NOTE_MS)
    return () => clearTimeout(t)
  }, [note])
  return note
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
  const note = useKairos()

  return (
    <div className="room">
      <main className="desktop">
        {windows.map(w => <Window key={w.id} w={w} focused={w.id === focusedId} />)}
      </main>
      <KairosNote note={note} />
      <Dock />
    </div>
  )
}
