// adapted from shannon-prime-kairos/ui/src/apps/panel.jsx
/* Shared panel furniture. Every app is a fetch, a loading state, an ERROR STATE,
 * and a body — and the error state is not optional. A panel that renders empty on
 * a failed fetch is indistinguishable from a panel whose subject is genuinely
 * empty, which is how "the data is gone" and "the endpoint 404'd" become the
 * same picture. */
import { useEffect, useRef, useState } from 'react'
import { State } from '../kit/parts.jsx'
import { createPoller } from './poller.js'

export function usePoll(fn, ms = 0) {
  const [state, set] = useState({ loading: true, data: null, error: null })
  const pollerRef = useRef(null)
  useEffect(() => {
    // One request in flight at a time, and refresh() supersedes anything in flight (poller.js).
    const p = createPoller(
      fn,
      d => set({ loading: false, data: d, error: null }),
      e => set(s => ({ loading: false, data: s.data, error: String(e.message || e) })))
    pollerRef.current = p
    p.tick()
    if (!ms) return () => p.stop()
    const t = setInterval(p.tick, ms)
    return () => { p.stop(); clearInterval(t) }
  }, [fn, ms])
  // A panel that WRITES re-reads at once: refresh() always fetches, and a poll that was
  // already in flight can no longer overwrite the newer answer.
  // The error keeps the last good `data`: a transient failed poll should not blank a
  // panel that was showing something a second ago.
  return { ...state, refresh: () => pollerRef.current && pollerRef.current.refresh() }
}

/* The states are the KIT'S: one loading voice and one error voice for every panel. The
 * error keeps saying what failed, verbatim. With data already on screen, a failed poll
 * shows that data plus a small inline error rather than blanking the panel. */
export function Body({ state, children }) {
  if (state.loading) return <State kind="loading" title="Loading…" />
  if (state.error && state.data == null) return <State kind="error" title="Could not load">{state.error}</State>
  return (
    <>
      {state.error ? <div role="alert" className="pn-err">Could not refresh: {state.error}</div> : null}
      {children(state.data)}
    </>
  )
}
