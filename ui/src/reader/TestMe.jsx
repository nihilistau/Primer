/* Test me — a panel under the page: three questions made from it (recall, explain why, and
 * connect to something read earlier when there is a trail). Making them is a real request at
 * the GPU gate, so it may wait while a picture is made. "New questions" once all are
 * answered. A reader reset forgets the questions: the panel says so and offers to make new
 * ones (it never asks the model by itself). Props: { nodeId, onClose }. */
import { useEffect, useRef, useState } from 'react'
import * as api from '../api.js'
import { Button, Chip, State } from '../kit/parts.jsx'
import { onReaderReset, quizErrorText } from '../apps/readerModel.js'
import QuestionCard from './QuestionCard.jsx'
import '../apps/apps.css'

export default function TestMe({ nodeId, onClose }) {
  const [items, setItems] = useState([])
  const [making, setMaking] = useState(false)
  const [err, setErr] = useState('')
  const [answered, setAnswered] = useState(() => new Set())
  const [batch, setBatch] = useState(0)            // remounts the cards for a new batch
  const [forgotten, setForgotten] = useState(false) // a reader reset deleted the questions
  const live = useRef(0)

  const make = async (fresh) => {
    const mine = ++live.current
    setMaking(true); setErr(''); setForgotten(false)
    try {
      const r = await api.quizMake(nodeId, fresh)
      if (mine !== live.current) return
      const list = r.items || []
      setItems(list)
      // a batch made earlier may already be answered: New questions is offered at once then
      setAnswered(new Set(list.filter(i => i.last_mark).map(i => i.id)))
      setBatch(b => b + 1)
    } catch (e) {
      if (mine === live.current) setErr(quizErrorText(e))
    } finally {
      if (mine === live.current) setMaking(false)
    }
  }
  useEffect(() => { make(false); return () => { live.current++ } }, [nodeId])   // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => onReaderReset(() => {
    live.current++
    setItems([]); setAnswered(new Set()); setMaking(false); setErr(''); setForgotten(true)
  }), [])

  const allDone = items.length > 0 && items.every(i => answered.has(i.id))

  return (
    <section className="qz" aria-label="Test me">
      <div className="qz-head">
        <h3 className="en-h">Test me</h3>
        <Button size="sm" variant="ghost" onClick={onClose}>Close</Button>
      </div>
      {making ? (
        <div className="qz-making" role="status">
          <Chip busy tone="accent" wrap>Writing questions… (waits for the GPU if a picture is being made)</Chip>
        </div>
      ) : null}
      {forgotten ? (
        <State title="Reset: these questions were forgotten" action={<Button size="sm" onClick={() => make(false)}>New questions</Button>} />
      ) : null}
      {err ? (
        <State kind="error" title="No questions" action={<Button size="sm" onClick={() => make(false)}>Try again</Button>}>{err}</State>
      ) : null}
      {!making && items.map((it, k) => (
        <QuestionCard key={`${batch}-${it.id}`} item={it} n={k + 1}
                      onMarked={() => setAnswered(s => new Set([...s, it.id]))} />
      ))}
      {allDone && !making ? (
        <div className="qz-row">
          <Button size="sm" onClick={() => make(true)}>New questions</Button>
          <span className="en-note">Answered questions come back in Review.</span>
        </div>
      ) : null}
    </section>
  )
}
