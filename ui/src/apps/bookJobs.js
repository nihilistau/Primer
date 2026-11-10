/* bookJobs — the Book window's two timed jobs, as plain objects (no React), so they can be
 * tested with fake timers and a stubbed api:
 *  - createPreviewer: the preview count, asked for `delay` ms after the last change; an older
 *    answer never overwrites a newer one (each request aborts the one before, and a sequence
 *    number drops anything late);
 *  - createFollower: the earlier books and the book being made. load() reads the list and picks
 *    up a book still being made; make() starts one and follows it, and on a 409 (one is being
 *    made already) reloads so the window follows that one; follow() polls a book to its end;
 *    stop() ends everything (the window closed). */
import { pollJob } from './imageJob.js'
import { isActive } from './book.js'

export const PREVIEW_MS = 400

/* `fetchPreview(body, signal)` -> promise; onResult({preview}) or onResult({error}); onPending()
 * when a request is scheduled. request(null) clears. */
export function createPreviewer({ fetchPreview, onResult, onPending = () => {}, delay = PREVIEW_MS }) {
  let seq = 0, timer = null, ac = null, alive = true
  const cancel = () => { clearTimeout(timer); timer = null; if (ac) ac.abort(); ac = null }
  return {
    request(body) {
      if (!alive) return
      const mine = ++seq
      cancel()
      if (!body) return
      onPending()
      const ctl = ac = new AbortController()
      timer = setTimeout(() => {
        timer = null
        fetchPreview(body, ctl.signal).then(
          (preview) => { if (alive && mine === seq) onResult({ preview }) },
          (error) => { if (alive && mine === seq && !ctl.signal.aborted) onResult({ error }) })
      }, delay)
    },
    stop() { alive = false; seq++; cancel() },
  }
}

/* api: {books, bookGet, bookMake}. Callbacks: onBooks(rows), onJob(book|null) (null: the book is
 * gone), onEnded(book) (done or failed), onError(kind, error) with kind 'list' | 'poll'. */
export function createFollower({ api, onBooks = () => {}, onJob = () => {}, onEnded = () => {}, onError = () => {},
                                 interval = 1000 }) {
  let alive = true, followingId = '', current = null, pollAc = null
  const self = {
    get id() { return followingId },
    follow(id) {
      if (!alive || !id) return
      if (pollAc) pollAc.abort()
      followingId = id
      const ac = pollAc = new AbortController()
      pollJob(id, (b) => { if (alive && !ac.signal.aborted) { current = b; onJob(b) } }, {
        interval, signal: ac.signal, fetchJob: api.bookGet,
        onError: (e) => {
          if (!alive || ac.signal.aborted) return
          if (e && e.status === 404) { if (followingId === id) { followingId = ''; current = null }; onJob(null) }
          else onError('poll', e)
        },
      }).then((b) => {
        if (b && alive && !ac.signal.aborted) { onEnded(b); self.load() }
      })
    },
    /* the book this follower is following was deleted: stop asking about it */
    drop(id) {
      if (id && id === followingId) { if (pollAc) pollAc.abort(); pollAc = null; followingId = ''; current = null }
    },
    async load() {
      try {
        const r = await api.books()
        if (!alive) return
        const rows = Array.isArray(r && r.books) ? r.books : []
        onBooks(rows)
        const active = rows.find(isActive)
        // follow it unless this window already follows a book still being made
        if (active && active.id !== followingId && !(followingId && isActive(current))) self.follow(active.id)
      } catch (e) {
        if (alive) onError('list', e)
      }
    },
    /* start a book; resolves with its id. A 409 reloads (to follow the one being made) and rethrows. */
    async make(body) {
      try {
        const r = await api.bookMake(body)
        if (!alive) return r && r.id
        current = { id: r.id, status: 'queued', title: body.title || '' }
        onJob(current)
        self.follow(r.id)
        self.load()
        return r.id
      } catch (e) {
        if (alive && e && e.status === 409) self.load()
        throw e
      }
    },
    stop() { alive = false; if (pollAc) pollAc.abort(); pollAc = null },
  }
  return self
}
