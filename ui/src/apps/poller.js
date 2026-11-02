/* poller — one fetch at a time, with generations. A tick while a request is still in
 * flight is skipped (never stack requests on a slow door). refresh() is different: the
 * caller just changed something, so it bumps the generation and always fetches, and any
 * response from an older generation is discarded rather than overwriting fresher state. */
export function createPoller(fn, onData, onError) {
  let alive = true
  let gen = 0
  let inflight = 0                       // requests of the current generation in flight
  const start = () => {
    const mine = gen
    inflight++
    fn()
      .then(d => { if (mine === gen) inflight--; if (alive && mine === gen) onData(d) })
      .catch(e => { if (mine === gen) inflight--; if (alive && mine === gen) onError(e) })
  }
  return {
    tick() { if (alive && !inflight) start() },
    refresh() { if (!alive) return; gen++; inflight = 0; start() },
    stop() { alive = false },
  }
}
