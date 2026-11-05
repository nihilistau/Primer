/* Reader — what Primer has learned about how you read: the summary (or why there is none
 * yet), the counted parts as small bars, what you know and found hard (from quizzes), your
 * own notes, the "Use this in pages" switch, Refresh and Reset. Re-reads once a second only
 * while a summary is being written. */
import { useEffect, useRef, useState } from 'react'
import * as api from '../api.js'
import { Button, Chip, TextArea } from '../kit/parts.jsx'
import { Body, usePoll } from './panel.jsx'
import { NOTES_CHARS, announceReaderReset, hostedNote, levelBars, shareBars, summaryLine, topicBars } from './readerModel.js'
import { errText } from './setupPlan.js'
import './apps.css'

const RESET_WORDS = 'This forgets how you read and your quiz history; pages stay.'

function Bar({ label, share, value, lean }) {
  return (
    <div className={'rm-bar' + (lean ? ' rm-bar-lean' : '')}>
      <span className="rm-bar-l">{label}</span>
      <span className="rm-bar-track" aria-hidden="true"><span className="rm-bar-fill" style={{ width: `${Math.round(share * 100)}%` }} /></span>
      <span className="rm-bar-v">{value}</span>
    </div>
  )
}

function Counted({ counted }) {
  const level = levelBars(counted.level)
  const styles = shareBars(counted.style)
  const topics = topicBars(counted.topics)
  return (
    <>
      <div className="rm-group">
        <div className="rm-sub">Level <span className="en-note">· {level.text}</span></div>
        {level.bars.map(b => <Bar key={b.key} label={b.label} share={b.share} value={b.n} lean={b.lean} />)}
      </div>
      <div className="rm-group">
        <div className="rm-sub">Branches</div>
        {styles.length
          ? styles.map(b => <Bar key={b.key} label={b.label} share={b.share} value={`${b.pct}%`} lean={b.lean} />)
          : <div className="en-note">No branches followed yet.</div>}
      </div>
      <div className="rm-group">
        <div className="rm-sub">Read most</div>
        {topics.length
          ? topics.map(t => <Bar key={t.title} label={t.title} share={t.share}
                                 value={[`${t.opens} open${t.opens === 1 ? '' : 's'}`, t.time].filter(Boolean).join(' · ')} />)
          : <div className="en-note">Nothing read yet.</div>}
      </div>
    </>
  )
}

function Notes({ saved, onSave, busy }) {
  const [text, setText] = useState(saved)
  useEffect(() => { setText(saved) }, [saved])
  const over = text.length > NOTES_CHARS
  return (
    <>
      <TextArea aria-label="Your notes" aria-describedby="rm-notes-count" rows={3} value={text}
                placeholder="In your own words: “I'm 12”, “skip the maths”…" onChange={(e) => setText(e.target.value)} />
      <div className="qz-row">
        <span id="rm-notes-count" className={'qz-count' + (over ? ' qz-over' : '')}>{`${text.length}/${NOTES_CHARS}`}</span>
        <Button size="sm" disabled={busy || over || text.trim() === saved} onClick={() => onSave(text.trim())}>Save</Button>
      </div>
    </>
  )
}

export function Model({ st, refresh }) {
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState({ at: '', text: '' })
  const [note, setNote] = useState('')                  // what Refresh answered
  const [confirm, setConfirm] = useState(false)
  const act = async (at, fn) => {
    setBusy(true); setErr({ at: '', text: '' })
    try { await fn() } catch (e) { setErr({ at, text: errText(e) }) }
    setBusy(false)
    refresh()
  }
  const line = summaryLine(st)
  const hosted = hostedNote(st)
  const Err = ({ at }) => (err.at === at && err.text ? <div className="en-err" role="alert">{err.text}</div> : null)

  return (
    <>
      <section className="en-sec">
        <h3 className="en-h">Summary</h3>
        <p className={'rm-summary' + (line.dim ? ' rm-dim' : '')}>{line.text}</p>
        {st.summary && st.error ? <div className="en-reason">{st.error}</div> : null}
        <div className="su-go">
          {st.writing ? <Chip busy tone="accent">writing…</Chip> : null}
          <Button size="sm" icon="reload" disabled={busy || st.writing}
                  onClick={() => act('summary', async () => {
                    setNote('')
                    const r = await api.refreshReader()
                    if (r.skipped) setNote(`Not refreshed: ${r.skipped}`)
                  })}>Refresh</Button>
          {note ? <span className="en-note">{note}</span> : null}
        </div>
        <Err at="summary" />
      </section>

      <section className="en-sec">
        <h3 className="en-h">Counted</h3>
        <Counted counted={st.counted || {}} />
      </section>

      <section className="en-sec rm-two">
        <div>
          <h3 className="en-h">Knows</h3>
          {(st.knows || []).length ? <ul className="rm-list">{st.knows.map(t => <li key={t}>{t}</li>)}</ul>
            : <div className="en-note">Nothing yet: right twice in a row in Review.</div>}
        </div>
        <div>
          <h3 className="en-h">Found hard</h3>
          {(st.hard || []).length ? <ul className="rm-list">{st.hard.map(t => <li key={t}>{t}</li>)}</ul>
            : <div className="en-note">Nothing yet.</div>}
        </div>
      </section>

      <section className="en-sec">
        <h3 className="en-h">Your notes</h3>
        <Notes saved={st.notes || ''} busy={busy} onSave={(notes) => act('notes', () => api.saveReader({ notes }))} />
        <div className="en-note">Your words always win over what Primer worked out.</div>
        <Err at="notes" />
      </section>

      <section className="en-sec">
        <label className="rm-switch">
          <input type="checkbox" role="switch" checked={!!st.enabled} disabled={busy}
                 onChange={(e) => act('switch', () => api.saveReader({ enabled: e.target.checked }))} />
          <span>Use this in pages</span>
        </label>
        {hosted ? <div className="en-reason">{hosted}</div> : null}
        <Err at="switch" />
      </section>

      <section className="en-sec">
        {!confirm ? (
          <Button size="sm" variant="danger" disabled={busy} onClick={() => setConfirm(true)}>Reset</Button>
        ) : (
          <div className="rm-confirm" role="group" aria-label="Confirm reset">
            <div className="en-reason">{RESET_WORDS}</div>
            <div className="su-go">
              <Button size="sm" variant="danger" disabled={busy}
                      onClick={() => act('reset', async () => { await api.resetReader(); announceReaderReset(); setConfirm(false); setNote('') })}>Reset</Button>
              <Button size="sm" variant="ghost" disabled={busy} onClick={() => setConfirm(false)}>Cancel</Button>
            </div>
          </div>
        )}
        <Err at="reset" />
      </section>
    </>
  )
}

export default function ReaderWindow() {
  const [ms, setMs] = useState(0)
  const poll = usePoll(api.readerState, ms)
  const writing = !!(poll.data && poll.data.writing)
  useEffect(() => { setMs(writing ? 1000 : 0) }, [writing])
  // a refresh starts writing a moment after it answers: look again shortly after acting
  const later = useRef(null)
  useEffect(() => () => clearTimeout(later.current), [])
  const refresh = () => { poll.refresh(); clearTimeout(later.current); later.current = setTimeout(poll.refresh, 600) }
  return (
    <div className="en rm">
      <Body state={poll}>{(st) => <Model st={st} refresh={refresh} />}</Body>
    </div>
  )
}
