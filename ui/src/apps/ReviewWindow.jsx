/* Review — the Test me questions that are due again (spaced: not yet tomorrow, partly in two
 * days, right moves up 1 → 3 → 7 → 21 → 60 days). Each is answered and marked like in Test
 * me, then says when it comes back. A page title opens that page in a reader. Loads once;
 * "Check again" (or a reader reset) re-reads. Marked cards stay on screen until then. */
import { useCallback, useEffect, useRef, useState } from 'react'
import * as api from '../api.js'
import { Button, State } from '../kit/parts.jsx'
import * as wm from '../windowManager.js'
import QuestionCard from '../reader/QuestionCard.jsx'
import { emptyReviewText, onReaderReset, quizErrorText } from './readerModel.js'
import './apps.css'

const openPage = (item) => wm.open('reader', { props: { start: { node_id: item.node_id } }, w: 720, h: 640 })

export default function ReviewWindow() {
  const [data, setData] = useState(null)        // {due, counts}
  const [loading, setLoading] = useState(true)
  const [err, setErr] = useState('')
  const [soonest, setSoonest] = useState(null)  // the soonest due_at among answers marked here
  const [marked, setMarked] = useState(0)
  const seq = useRef(0)

  const load = useCallback(async () => {
    const mine = ++seq.current
    setLoading(true); setErr('')
    try {
      const r = await api.quizDue(20)
      if (mine === seq.current) { setData(r); setMarked(0) }
    } catch (e) {
      if (mine === seq.current) setErr(quizErrorText(e))
    } finally {
      if (mine === seq.current) setLoading(false)
    }
  }, [])
  useEffect(() => { load(); return () => { seq.current++ } }, [load])
  useEffect(() => onReaderReset(() => { setSoonest(null); load() }), [load])

  const onMarked = (r) => {
    setMarked(n => n + 1)
    const at = r && r.item && r.item.due_at
    if (Number.isFinite(at)) setSoonest(s => (s === null || at < s ? at : s))
  }

  const due = (data && data.due) || []
  const counts = (data && data.counts) || { due: 0, total: 0 }
  return (
    <div className="en rv">
      <div className="rv-head">
        <span className="en-reason">
          {data ? `${counts.due} due${counts.due > due.length ? ` (showing ${due.length})` : ''} · ${counts.total} in all` : ''}
        </span>
        <Button size="sm" icon="reload" disabled={loading} onClick={load}>Check again</Button>
      </div>
      {err ? <div className="en-err" role="alert">{data ? `Could not refresh: ${err}` : err}</div> : null}
      {loading && !data ? <State kind="loading" title="Loading…" /> : null}
      {data && !due.length ? <State title={emptyReviewText(counts, soonest, Date.now())} /> : null}
      {due.map(it => <QuestionCard key={it.id} item={it} onOpenPage={openPage} showDue onMarked={onMarked} />)}
      {due.length && marked >= due.length
        ? <div className="en-note rv-done">All marked. Check again for more.</div>
        : null}
    </div>
  )
}
