/* Tale — the continuing story. With no tale yet: a seed box (or Invent one for me). With one:
 * its title, Next chapter with the pages it will draw on (tick boxes; add a page from the
 * trail), the chapters (each opens in the Story window; its pages open in a reader), the story
 * bible (the hero editable; places and threads removable) and the older tales. GET /v1/tales
 * is read on open and after every action, and every 1.5 s only while a chapter is being
 * written or the bible updated. Model text is shown as text. */
import { useEffect, useState } from 'react'
import * as api from '../api.js'
import { Button, Chip, Input, Select, TextArea } from '../kit/parts.jsx'
import * as wm from '../windowManager.js'
import { Body, usePoll } from './panel.jsx'
import {
  SEED_MAX, TALE_EVENT, announceTale, bibleLock, bibleRows, heroGender, heroName, canNext, chapterLabel, pollMs,
  previewLabel, progressLabel, seedCheck, taleErrorText, trailPages,
} from './tale.js'
import './apps.css'

const STARTING = 'Writing the start of the tale… (waits for the GPU if a picture is being made)'
const KEEP = 'The current tale is kept; you can reopen it.'
const HERO_MAX = { name: 60, description: 300, look: 200 }
const HERO_FIELDS = ['name', 'gender', 'description', 'look']

const openPage = (nodeId) => wm.open('reader', { props: { start: { node_id: nodeId } }, w: 720, h: 640 })
const openChapter = (ch, taleTitle) => wm.open('story', {
  props: { storyId: ch.story_id, subject: ch.title, kind: 'chapter', chapter: { n: ch.n, taleTitle } }, w: 720, h: 740,
})

/* run an action: busy while it runs, its error kept; after it, TALE_EVENT re-reads the state
 * here and in the dock */
function useAction() {
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState('')
  const run = async (fn) => {
    setBusy(true); setErr('')
    let ok = false
    try { await fn(); ok = true } catch (e) { setErr(taleErrorText(e)) }
    setBusy(false)
    announceTale()
    return ok
  }
  return { busy, err, run, setErr }
}

function StartTale({ onCancel }) {
  const [seed, setSeed] = useState('')
  const { busy, err, run } = useAction()
  const check = seedCheck(seed)
  const start = (s) => run(() => api.taleStart(s)).then(ok => { if (ok && onCancel) onCancel() })
  return (
    <section className="en-sec">
      <h3 className="en-h">A new tale</h3>
      {onCancel ? <p className="en-reason">{KEEP}</p> : null}
      <label className="tl-label" htmlFor="tl-seed">Who is the story about?</label>
      <TextArea id="tl-seed" rows={3} value={seed} disabled={busy} maxLength={SEED_MAX + 100}
                placeholder="e.g. a girl called Nell who builds things" onChange={(e) => setSeed(e.target.value)} />
      <div className="qz-row">
        <span className={'qz-count' + (seed.length > SEED_MAX ? ' qz-over' : '')}>{check.count}</span>
        <Button size="sm" variant={onCancel ? 'secondary' : 'primary'} disabled={busy || !check.ok} title={check.ok ? undefined : check.reason}
                onClick={() => start(seed.trim())}>Start the tale</Button>
        <Button size="sm" disabled={busy} onClick={() => start('')}>Invent one for me</Button>
        {onCancel ? <Button size="sm" variant="ghost" disabled={busy} onClick={onCancel}>Cancel</Button> : null}
      </div>
      {seed.length > SEED_MAX ? <div className="en-note qz-over">{check.reason}</div> : null}
      {busy ? <div className="tl-busy"><Chip busy tone="accent" wrap>{STARTING}</Chip></div> : null}
      {err ? <div className="en-err" role="alert">{err}</div> : null}
    </section>
  )
}

function NextChapter({ state }) {
  const cur = state.current
  const suggested = (state.preview && state.preview.pages) || []
  const [extra, setExtra] = useState([])            // pages added from the trail [{node_id, title}]
  const [off, setOff] = useState(() => new Set())   // node ids unticked
  const [trail, setTrail] = useState(null)          // the picker's pages, or null when closed
  const [trailErr, setTrailErr] = useState('')
  const { busy, err, run } = useAction()

  const pages = [...suggested, ...extra.filter(p => !suggested.some(s => s.node_id === p.node_id))]
  const chosen = pages.filter(p => !off.has(p.node_id)).map(p => p.node_id)
  const can = canNext(state, chosen)
  const progress = progressLabel(cur)

  const toggle = (id) => setOff(s => { const n = new Set(s); if (n.has(id)) n.delete(id); else n.add(id); return n })
  const openTrail = async () => {
    setTrailErr('')
    try { setTrail(trailPages(await api.graph(), pages.map(p => p.node_id))) } catch (e) { setTrailErr(taleErrorText(e)) }
  }
  const add = (p) => {
    setExtra(x => [...x, p])
    setOff(s => { const n = new Set(s); n.delete(p.node_id); return n })
    setTrail(t => (t ? t.filter(q => q.node_id !== p.node_id) : t))
  }
  const next = () => run(async () => {
    const r = await api.taleNext(chosen)
    setExtra([]); setOff(new Set()); setTrail(null)
    if (r && r.story_id) openChapter({ story_id: r.story_id, n: r.n, title: '' }, cur.title)
  })

  return (
    <section className="en-sec">
      <h3 className="en-h">Next chapter</h3>
      <p className="en-reason">{previewLabel(state.preview)}</p>
      {pages.length ? (
        <ul className="tl-pages" aria-label="Pages for the next chapter">
          {pages.map(p => (
            <li key={p.node_id}>
              <label className="tl-check">
                <input type="checkbox" checked={!off.has(p.node_id)} disabled={busy} onChange={() => toggle(p.node_id)} />
                <span>{p.title || p.node_id}</span>
              </label>
            </li>
          ))}
        </ul>
      ) : null}
      <div className="su-go">
        <Button size="sm" variant="primary" disabled={busy || !can.ok} title={can.ok ? undefined : can.reason}
                onClick={next}>{busy ? 'Writing…' : 'Next chapter'}</Button>
        <Button size="sm" variant="ghost" disabled={busy} onClick={trail ? () => setTrail(null) : openTrail}>
          {trail ? 'Close the trail' : 'Add a page from your trail'}
        </Button>
      </div>
      {!can.ok && !busy && !progress ? <div className="en-note">{can.reason}</div> : null}
      {trail ? (
        trail.length ? (
          <ul className="tl-trail" aria-label="Pages from your trail">
            {trail.map(p => (
              <li key={p.node_id}>
                <span className="tl-trail-t">{p.title}</span>
                <Button size="sm" variant="ghost" aria-label={`Add ${p.title}`} onClick={() => add(p)}>Add</Button>
              </li>
            ))}
          </ul>
        ) : <div className="en-note">No other pages on your trail yet.</div>
      ) : null}
      {trailErr ? <div className="en-err" role="alert">{trailErr}</div> : null}
      {busy || progress ? (
        <div className="tl-busy" role="status">
          <Chip busy tone="accent" wrap>{progress || `Writing chapter ${(cur.chapters || []).length + 1}…`}</Chip>
        </div>
      ) : null}
      {err ? <div className="en-err" role="alert">{err}</div> : null}
    </section>
  )
}

function Chapter({ ch, tale }) {
  const { busy, err, run } = useAction()
  return (
    <li className="tl-ch">
      <button type="button" className="tl-ch-open" onClick={() => openChapter(ch, tale.title)}>{chapterLabel(ch)}</button>
      {(ch.pages || []).length ? (
        <div className="tl-ch-pages">
          <span className="en-note">From</span>
          {ch.pages.map(p => (
            <Button key={p.node_id} size="sm" variant="ghost" onClick={() => openPage(p.node_id)}>{p.title || p.node_id}</Button>
          ))}
        </div>
      ) : null}
      {ch.summary ? <p className="tl-summary">{ch.summary}</p> : null}
      {ch.update_failed ? (
        <div className="tl-failed">
          <span>The story bible wasn’t updated</span>
          <Button size="sm" disabled={busy || tale.writing || tale.updating} aria-label={`Retry chapter ${ch.n}`}
                  title={bibleLock(tale) || undefined}
                  onClick={() => run(() => api.taleRetry(tale.id, ch.n))}>Retry</Button>
        </div>
      ) : null}
      {err ? <div className="en-err" role="alert">{err}</div> : null}
    </li>
  )
}

function Hero({ tale }) {
  const hero = (tale.bible && tale.bible.hero) || {}
  const [edit, setEdit] = useState(null)       // {name, gender, description, look} while editing
  const { busy, err, run, setErr } = useAction()
  const lock = bibleLock(tale)
  const save = () => {
    const fields = {}
    for (const k of HERO_FIELDS) if (edit[k] !== (hero[k] || '')) fields[k] = edit[k]
    if (!Object.keys(fields).length) { setEdit(null); return }
    run(() => api.taleEdit(tale.id, fields)).then(ok => { if (ok) setEdit(null) })
  }
  if (!edit) {
    return (
      <div className="tl-hero">
        <div className="tl-hero-name">{heroName(tale)}</div>
        {heroGender(tale) ? <div className="tl-hero-line">{heroGender(tale)}</div> : null}
        {hero.description ? <div className="tl-hero-line">{hero.description}</div> : null}
        {hero.look ? <div className="tl-hero-line"><span className="qz-answer-h">Look</span> {hero.look}</div> : null}
        <div className="su-go">
          <Button size="sm" variant="ghost" onClick={() => { setErr(''); setEdit(Object.fromEntries(HERO_FIELDS.map(k => [k, hero[k] || '']))) }}>Edit the hero</Button>
        </div>
      </div>
    )
  }
  const field = (k, label, Comp, rows) => (
    <label className="tl-field">
      <span className="tl-label">{label} <span className="qz-count">{edit[k].length} / {HERO_MAX[k]}</span></span>
      <Comp value={edit[k]} maxLength={HERO_MAX[k]} disabled={busy} rows={rows}
            onChange={(e) => setEdit(x => ({ ...x, [k]: e.target.value }))} />
    </label>
  )
  return (
    <div className="tl-hero">
      {field('name', 'Name', Input)}
      <label className="tl-field">
        <span className="tl-label">Gender</span>
        <Select value={edit.gender} disabled={busy}
                onChange={(e) => setEdit(x => ({ ...x, gender: e.target.value }))}>
          <option value="">Not set</option>
          <option value="female">Female</option>
          <option value="male">Male</option>
        </Select>
      </label>
      {field('description', 'Description', TextArea, 2)}
      {field('look', 'Look (starts every picture)', TextArea, 2)}
      <div className="su-go">
        <Button size="sm" variant="primary" disabled={busy || !!lock || !edit.name.trim()} title={lock || undefined}
                onClick={save}>Save</Button>
        <Button size="sm" variant="ghost" disabled={busy} onClick={() => setEdit(null)}>Cancel</Button>
      </div>
      {lock ? <div className="en-note">{lock}; save when it’s done.</div> : null}
      {err ? <div className="en-err" role="alert">{err}</div> : null}
    </div>
  )
}

function Bible({ tale }) {
  const { busy, err, run } = useAction()
  const lock = bibleLock(tale)
  return (
    <section className="en-sec">
      <h3 className="en-h">Story bible</h3>
      <Hero tale={tale} />
      {bibleRows(tale.bible).map(sec => (
        <div key={sec.key} className="rm-group">
          <div className="rm-sub">{sec.label}</div>
          {sec.items.length ? (
            <ul className="tl-items">
              {sec.items.map((it, i) => (
                <li key={i}>
                  <span className="tl-item-t">{it.text}</span>
                  {it.detail ? <span className="tl-item-d">{it.detail}</span> : null}
                  {it.remove ? (
                    <Button size="sm" variant="ghost" disabled={busy || !!lock} aria-label={`Remove ${it.text}`}
                            title={lock || undefined}
                            onClick={() => run(() => api.taleEdit(tale.id, it.remove))}>Remove</Button>
                  ) : null}
                </li>
              ))}
            </ul>
          ) : <div className="en-note">None yet.</div>}
        </div>
      ))}
      {lock ? <div className="en-note">{lock}; places and threads can be removed when it’s done.</div> : null}
      {err ? <div className="en-err" role="alert">{err}</div> : null}
    </section>
  )
}

function Older({ state }) {
  const curId = state.current && state.current.id
  const older = (state.tales || []).filter(t => t.id !== curId)
  const { busy, err, run } = useAction()
  if (!older.length) return null
  return (
    <section className="en-sec">
      <h3 className="en-h">Older tales</h3>
      <ul className="tl-items">
        {older.map(t => (
          <li key={t.id}>
            <span className="tl-item-t">{t.title || 'Untitled'}</span>
            <span className="tl-item-d">{t.chapters} {t.chapters === 1 ? 'chapter' : 'chapters'}</span>
            <Button size="sm" variant="ghost" disabled={busy || !!(state.current && state.current.writing)}
                    aria-label={`Open ${t.title || 'Untitled'}`} onClick={() => run(() => api.taleOpen(t.id))}>Open</Button>
          </li>
        ))}
      </ul>
      {err ? <div className="en-err" role="alert">{err}</div> : null}
    </section>
  )
}

export function Tale({ state }) {
  const [anew, setAnew] = useState(false)
  const cur = state.current
  if (!cur) {
    return (
      <>
        <StartTale />
        <Older state={state} />
      </>
    )
  }
  const chapters = cur.chapters || []
  return (
    <>
      <section className="en-sec tl-head">
        <h2 className="tl-title">{cur.title}</h2>
        {!anew ? (
          <Button size="sm" variant="ghost" disabled={cur.writing}
                  onClick={() => { if (window.confirm(KEEP)) setAnew(true) }}>Start a new tale</Button>
        ) : null}
      </section>
      {anew ? <StartTale onCancel={() => setAnew(false)} /> : null}
      <NextChapter key={cur.id} state={state} />
      <section className="en-sec">
        <h3 className="en-h">Chapters</h3>
        {chapters.length ? (
          <ol className="tl-chapters">
            {chapters.map(ch => <Chapter key={ch.n} ch={ch} tale={cur} />)}
          </ol>
        ) : <div className="en-note">No chapters yet. Read a page or two, then press Next chapter.</div>}
      </section>
      <Bible tale={cur} />
      <Older state={state} />
    </>
  )
}

export default function TaleWindow() {
  const [ms, setMs] = useState(0)
  const poll = usePoll(api.tales, ms)
  const cur = poll.data && poll.data.current
  const every = pollMs(cur)   // quick while writing, slow while a chapter is told, else none
  useEffect(() => { setMs(every) }, [every])
  const { refresh } = poll
  useEffect(() => {
    window.addEventListener(TALE_EVENT, refresh)
    return () => window.removeEventListener(TALE_EVENT, refresh)
  }, [])   // eslint-disable-line react-hooks/exhaustive-deps
  return (
    <div className="en tl">
      <Body state={poll}>{(d) => <Tale state={d} />}</Body>
    </div>
  )
}
