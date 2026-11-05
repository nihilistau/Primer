/* One question with an answer box: Check marks it (Right / Partly / Not yet, with a line of
 * feedback quoting the page), Show answer fetches the model answer. Check after Show answer
 * is not yet without asking the model (the card says so first). Test me and Review both
 * use it. Props: { item, n?, onOpenPage?(item) (Review: the page title opens it),
 * showDue? (after marking, when it comes back), onMarked?(resp) }. */
import { useState } from 'react'
import * as api from '../api.js'
import { Button, Chip, TextArea } from '../kit/parts.jsx'
import { ANSWER_CHARS, REVEALED_NOTE, announceQuiz, answerCheck, dueLabel, markLabel, quizErrorText } from '../apps/readerModel.js'

const KIND_WORDS = { recall: 'Recall', why: 'Explain why', connect: 'Connect' }

export default function QuestionCard({ item, n, onOpenPage, showDue, onMarked }) {
  const [text, setText] = useState('')
  const [busy, setBusy] = useState('')            // 'check' | 'show' while a request runs
  const [result, setResult] = useState(null)      // POST /v1/quiz/answer's reply
  const [answer, setAnswer] = useState('')        // the model answer, once shown
  const [err, setErr] = useState('')
  const check = answerCheck(text)
  const mark = result ? markLabel(result.mark) : null
  const idBase = `qc-${item.id}`

  const submit = async () => {
    if (!check.ok || busy) return
    setBusy('check'); setErr('')
    try {
      const r = await api.quizAnswer(item.id, text, !!answer)
      setResult(r)
      announceQuiz()
      if (onMarked) onMarked(r)
    } catch (e) { setErr(quizErrorText(e)) }
    setBusy('')
  }
  const show = async () => {
    if (busy) return
    if (result && result.answer) { setAnswer(result.answer); return }
    setBusy('show'); setErr('')
    try { const r = await api.quizItem(item.id); setAnswer(r.answer || '') } catch (e) { setErr(quizErrorText(e)) }
    setBusy('')
  }

  return (
    <div className="qz-card">
      <div className="qz-top">
        {n ? <span className="qz-n">{n}</span> : null}
        <Chip>{KIND_WORDS[item.kind] || item.kind}</Chip>
        {onOpenPage
          ? <button type="button" className="qz-page" onClick={() => onOpenPage(item)} title="Open this page in a reader">{item.title}</button>
          : null}
        {!result && item.last_mark ? <span className="en-note">last time: {markLabel(item.last_mark).text.toLowerCase()}</span> : null}
      </div>
      <div className="qz-q" id={idBase + '-q'}>{item.question}</div>
      <TextArea aria-labelledby={idBase + '-q'} aria-describedby={idBase + '-c'} value={text} rows={3}
                readOnly={!!result} disabled={busy === 'check'}
                onChange={(e) => setText(e.target.value)} maxLength={ANSWER_CHARS + 200} placeholder="Your answer…" />
      <div className="qz-row">
        <span className={'qz-count' + (text.length > ANSWER_CHARS ? ' qz-over' : '')} id={idBase + '-c'}>{check.count}</span>
        {!result ? (
          <Button size="sm" variant="primary" disabled={!check.ok || !!busy} title={check.ok ? undefined : check.reason}
                  onClick={submit}>{busy === 'check' ? 'Checking…' : 'Check'}</Button>
        ) : null}
        {!answer ? <Button size="sm" disabled={!!busy} onClick={show}>{busy === 'show' ? 'Opening…' : 'Show answer'}</Button> : null}
      </div>
      {result ? (
        <div className="qz-result" role="status">
          <Chip tone={mark.tone} dot>{mark.text}</Chip>
          <span className="qz-fb">{result.feedback}</span>
          {showDue && result.item ? <span className="en-note">{dueLabel(result.item, Date.now())}</span> : null}
        </div>
      ) : null}
      {answer ? <div className="qz-answer"><span className="qz-answer-h">Answer</span> {answer}</div> : null}
      {answer && !result ? <div className="en-note">{REVEALED_NOTE}</div> : null}
      {err ? <div className="en-err" role="alert">{err}</div> : null}
    </div>
  )
}
