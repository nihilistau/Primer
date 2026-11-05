/* Quest — a run through rooms, one per page read lately (oldest first). Each room is the
 * page described as a place; its door asks one question about it. Right opens the door; a
 * miss says only "Not yet" and gives the hint (feedback would give the answer away), two
 * point back to the page, three offer Skip. Later rooms stay locked. A quest being played
 * can be left. Polls once a second only while the rooms are being built; a reader reset
 * re-reads. Props: { fromNodeId? } (offer to build from that page's trail). */
import { useEffect, useState } from 'react'
import * as api from '../api.js'
import { Button, Chip, TextArea } from '../kit/parts.jsx'
import * as wm from '../windowManager.js'
import { Body, usePoll } from './panel.jsx'
import { ANSWER_CHARS, answerCheck, markLabel, onReaderReset, questErrorText, questProgress, questRoomView } from './readerModel.js'
import './apps.css'

const REREAD = 'Reread the page: the answer is in it.'
const openPage = (nodeId) => wm.open('reader', { props: { start: { node_id: nodeId } }, w: 720, h: 640 })

function Passed({ r }) {
  return (
    <li className="qs-room qs-passed">
      <div className="qs-room-top">
        <span className="qs-n">{r.index + 1}</span>
        <span className="qs-title">{r.title}</span>
        <Chip tone={r.status === 'opened' ? 'ok' : 'neutral'} dot>{r.status === 'opened' ? 'opened' : 'skipped'}</Chip>
      </div>
      <div className="qs-desc">{r.description}</div>
      <div className="qs-q">{r.question}</div>
      {r.answer ? <div className="qz-answer"><span className="qz-answer-h">Answer</span> {r.answer}</div> : null}
    </li>
  )
}

function Current({ r, questId, last, onDone }) {
  const [text, setText] = useState('')
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState('')
  const check = answerCheck(text)
  const mark = last ? markLabel('not_yet') : null   // a miss: partly says no more than not yet
  const act = async (fn) => {
    setBusy(true); setErr('')
    try { await fn() } catch (e) { setErr(questErrorText(e)) }
    setBusy(false)
  }
  const tryDoor = () => act(async () => {
    onDone(await api.questAnswer(questId, text))   // a miss keeps the words, to be mended
  })
  const qid = `qs-q-${questId}-${r.index}`
  return (
    <li className="qs-room qs-current" aria-current="step">
      <div className="qs-room-top">
        <span className="qs-n">{r.index + 1}</span>
        <span className="qs-title">{r.title}</span>
        {r.tries ? <span className="en-note">{r.tries} {r.tries === 1 ? 'try' : 'tries'}</span> : null}
      </div>
      <div className="qs-desc">{r.description}</div>
      <div className="qs-q" id={qid}>{r.question}</div>
      <TextArea aria-labelledby={qid} rows={3} value={text} disabled={busy} maxLength={ANSWER_CHARS + 200}
                placeholder="Your answer…" onChange={(e) => setText(e.target.value)} />
      <div className="qz-row">
        <span className={'qz-count' + (text.length > ANSWER_CHARS ? ' qz-over' : '')}>{check.count}</span>
        <Button size="sm" variant="primary" disabled={busy || !check.ok} title={check.ok ? undefined : check.reason}
                onClick={tryDoor}>{busy ? 'Trying…' : 'Try the door'}</Button>
        {r.canSkip ? <Button size="sm" disabled={busy} onClick={() => act(async () => onDone(await api.questSkip(questId)))}>Skip this room</Button> : null}
      </div>
      {last ? (
        <div className="qz-result" role="status">
          <Chip tone={mark.tone} dot>{mark.text}</Chip>
        </div>
      ) : null}
      {r.hint ? <div className="qs-hint"><span className="qz-answer-h">Hint</span> {r.hint}</div> : null}
      {r.reread ? (
        <div className="qs-hint">
          {REREAD}{' '}
          <Button size="sm" variant="ghost" onClick={() => openPage(r.nodeId)}>Open the page</Button>
        </div>
      ) : null}
      {err ? <div className="en-err" role="alert">{err}</div> : null}
    </li>
  )
}

export function Run({ q, refresh, fromNodeId }) {
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState('')
  const [last, setLast] = useState(null)        // {questId, index, mark}: the last door tried, missed
  const build = async (from) => {
    setBusy(true); setErr(''); setLast(null)
    try { await api.questBuild(from) } catch (e) { setErr(questErrorText(e)) }
    setBusy(false)
    refresh()
  }
  const leave = async () => {
    setBusy(true); setErr(''); setLast(null)
    try { await api.questAbandon(q.id) } catch (e) { setErr(questErrorText(e)) }
    setBusy(false)
    refresh()
  }
  const onDone = (res) => {
    const idx = q.at_room
    setLast(res.opened || res.skipped ? null : { questId: q.id, index: idx, mark: res.mark })
    refresh()
  }
  const builder = (label) => (
    <div className="su-go">
      <Button variant="primary" disabled={busy} onClick={() => build()}>{label}</Button>
      {fromNodeId ? <Button disabled={busy} onClick={() => build(fromNodeId)}>Build from this page’s trail</Button> : null}
    </div>
  )

  if (!q) {
    return (
      <section className="en-sec">
        <p className="rm-summary">A quest walks you through the pages you read lately, one room each. Every door asks one question about its page.</p>
        {builder('Build a quest')}
        {err ? <div className="en-err" role="alert">{err}</div> : null}
      </section>
    )
  }
  const rooms = (q.rooms || []).map(questRoomView)
  return (
    <>
      {q.status === 'building' ? (
        <section className="en-sec">
          <Chip busy tone="accent" wrap>{questProgress(q)}</Chip>
          {q.building && q.building.total ? <progress className="su-bar" max={q.building.total} value={q.building.done || 0} aria-label="Building progress" /> : null}
        </section>
      ) : null}
      {q.status === 'failed' ? (
        <section className="en-sec">
          <div className="en-err" role="alert">This quest could not be built: {q.error || 'unknown error'}</div>
          {builder('Try again')}
        </section>
      ) : null}
      {q.status === 'active' || q.status === 'finished' ? (
        <section className="en-sec">
          {q.status === 'active' ? (
            <div className="su-go">
              <Button size="sm" variant="ghost" disabled={busy} onClick={leave}>Leave this quest</Button>
              {fromNodeId ? <Button size="sm" variant="ghost" disabled={busy} onClick={() => build(fromNodeId)}>Build from this page’s trail</Button> : null}
            </div>
          ) : null}
          <ol className="qs-path" aria-label="Rooms">
            {rooms.map(r => r.kind === 'passed' ? <Passed key={r.index} r={r} />
              : r.kind === 'current' && q.status === 'active'
                ? <Current key={`${q.id}-${r.index}`} r={r} questId={q.id} onDone={onDone}
                           last={last && last.questId === q.id && last.index === r.index ? last : null} />
                : <li key={r.index} className="qs-room qs-locked"><span className="qs-n">{r.index + 1}</span> <span className="en-note">locked</span></li>)}
          </ol>
        </section>
      ) : null}
      {q.status === 'finished' ? (
        <section className="en-sec">
          <h3 className="en-h">Through</h3>
          <p className="rm-summary">
            {q.summary
              ? `${q.summary.opened} of ${q.summary.rooms} doors opened${q.summary.skipped ? `, ${q.summary.skipped} skipped` : ''}.`
              : 'The last door is open.'}
          </p>
          {builder('Build another')}
        </section>
      ) : null}
      {err ? <div className="en-err" role="alert">{err}</div> : null}
    </>
  )
}

export default function QuestWindow({ fromNodeId }) {
  const [ms, setMs] = useState(0)
  const poll = usePoll(api.quest, ms)
  const status = poll.data && poll.data.quest && poll.data.quest.status
  useEffect(() => { setMs(status === 'building' ? 1000 : 0) }, [status])
  const { refresh } = poll
  useEffect(() => onReaderReset(refresh), [])   // eslint-disable-line react-hooks/exhaustive-deps
  return (
    <div className="en qs">
      <Body state={poll}>{(d) => <Run q={d.quest} refresh={poll.refresh} fromNodeId={fromNodeId} />}</Body>
    </div>
  )
}
