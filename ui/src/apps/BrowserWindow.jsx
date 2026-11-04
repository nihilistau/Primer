/* BrowserWindow — a reader's browser: web pages and offline Wikipedia articles as clean
 * Markdown with links and pictures. Address bar, back/forward/reload, a selection menu
 * (Explore, Ask), and a side panel with Ask (about this page) and Click through (Primer
 * follows links toward a goal, the window going where it goes). Make a page opens a Primer
 * page grounded in this page alone. Props: { windowId, start? } (an address to open first). */
import { useCallback, useEffect, useRef, useState } from 'react'
import * as api from '../api.js'
import * as wm from '../windowManager.js'
import { Button, Chip, Input, State } from '../kit/parts.jsx'
import { Markdown } from '../reader/markdown.jsx'
import { safeHref } from '../reader/citations.js'
import { OPEN_BROWSER_EVENT, openBrowser } from '../openBrowser.js'
import { addressFromInput, clampX, createNav, pageConcept, stepLink, stepTarget } from './browserNav.js'
import { askPrefill, cleanSelection, selectionMenuItems } from './selection.js'
import './apps.css'

const whereName = (w) => (w === 'reference' ? 'offline encyclopedia' : 'web')

export default function BrowserWindow({ windowId, start }) {
  const nav = useRef(createNav())
  const [, tick] = useState(0)
  const [addr, setAddr] = useState(start || '')
  const [view, setView] = useState(null)         // a page {u, title, markdown, where, origin_href} or {search, where, results}
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [st, setSt] = useState(null)             // /v1/status
  const loadId = useRef(0)
  const loadAbort = useRef(null)
  const bodyRef = useRef(null)
  const askRef = useRef(null)
  const [menu, setMenu] = useState(null)         // {text, x, y} while the selection menu shows

  /* side panel */
  const [tab, setTab] = useState('ask')
  const [question, setQuestion] = useState('')
  const [ask, setAsk] = useState({ running: false, text: '', error: '' })
  const askAbort = useRef(null)
  const [goal, setGoal] = useState('')
  const [go, setGo] = useState({ running: false, steps: [], answer: '', note: '', error: '', waiting: false })
  const goAbort = useRef(null)

  useEffect(() => { api.status().then(setSt).catch(() => setSt(null)) }, [])

  const stopAsk = () => { if (askAbort.current) askAbort.current.abort(); askAbort.current = null }
  const stopGo = () => { if (goAbort.current) goAbort.current.abort(); goAbort.current = null }
  useEffect(() => () => { stopAsk(); stopGo(); loadId.current++; if (loadAbort.current) loadAbort.current.abort() }, [])

  /* Moving this window anywhere ends an answer in flight; a click-through ends too unless
   * it is the click-through itself moving (byAgent). */
  const settle = (byAgent) => {
    if (!byAgent && goAbort.current) { stopGo(); setGo(g => ({ ...g, running: false, waiting: false, note: 'Stopped.' })) }
    stopAsk(); setAsk({ running: false, text: '', error: '' })
    setMenu(null)
  }

  /* Open an address in this window. mode: 'push' (a new history step), 'none' (back/forward),
   * 'replace' (reload). */
  const begin = (byAgent) => {
    settle(byAgent)
    if (loadAbort.current) loadAbort.current.abort()
    const ac = new AbortController(); loadAbort.current = ac
    setLoading(true); setError('')
    return { id: ++loadId.current, signal: ac.signal }
  }
  // a failed open leaves the address bar showing the page that is still on screen
  const restoreAddr = () => { const c = nav.current.current(); if (c) setAddr(addrOf(c)) }
  /* mode 'back'/'forward' move the history only once the page has opened */
  const commit = (mode, entry) => {
    if (mode === 'push') nav.current.push(entry)
    else { if (mode === 'back') nav.current.move(-1); else if (mode === 'forward') nav.current.move(1); nav.current.replace(entry) }
  }
  const load = useCallback(async (u, mode = 'push', byAgent = false) => {
    const { id, signal } = begin(byAgent)
    setAddr(u)
    try {
      const p = await api.browse(u, signal)
      if (id !== loadId.current) return
      const entry = { u: p.final_url || u, title: p.title }
      commit(mode, entry)
      setView({ ...p, u: entry.u })
      setAddr(entry.u)
      if (bodyRef.current) bodyRef.current.scrollTop = 0
    } catch (e) {
      if (id !== loadId.current) return
      setError(e.message || 'That page could not be opened.')
      restoreAddr()
    }
    if (id === loadId.current) setLoading(false)
    tick(x => x + 1)
  }, [])

  const search = useCallback(async (q, where, mode = 'push') => {
    const { id, signal } = begin(false)
    try {
      const r = await api.browseSearch(q, where, signal)
      if (id !== loadId.current) return
      commit(mode, { u: `search:${where}:${q}`, title: `Search: ${q}`, search: { q, where } })
      setView({ search: q, where, results: r.results || [] })
    } catch (e) {
      if (id !== loadId.current) return
      setError(e.message || 'The search failed.')
      restoreAddr()
    }
    if (id === loadId.current) setLoading(false)
    tick(x => x + 1)
  }, [])

  const show = (entry, mode) => {
    if (!entry) return
    if (entry.search) { setAddr(entry.search.q); search(entry.search.q, entry.search.where, mode) } else load(entry.u, mode)
  }
  const addrOf = (entry) => (entry.search ? entry.search.q : entry.u)
  const submit = (e) => {
    e.preventDefault()
    const r = addressFromInput(addr, st)
    if (r.error) { setError(r.error); return }
    if (r.u) load(r.u); else search(r.search, r.where)
  }
  const back = () => show(nav.current.peek(-1), 'back')
  const forward = () => show(nav.current.peek(1), 'forward')
  const reload = () => { const c = nav.current.current(); if (c) show(c, 'replace') }

  /* a link on the page: here, or (ctrl/middle) a new Browser window */
  const follow = (u, { newWindow }) => { if (newWindow) openBrowser(u, { newWindow: true }); else load(u) }

  const first = useRef(false)
  useEffect(() => { if (!first.current && start) { first.current = true; load(start) }
    return () => { first.current = false }    // StrictMode mounts twice: the second mount must load too
  }, [start, load])
  // another window asked the most recent Browser to open an address
  useEffect(() => {
    const on = (e) => {
      const d = e.detail
      if (!d || d.handled || d.windowId !== windowId) return
      d.handled = true
      load(d.u)
    }
    window.addEventListener(OPEN_BROWSER_EVENT, on)
    return () => window.removeEventListener(OPEN_BROWSER_EVENT, on)
  }, [windowId, load])
  useEffect(() => { if (view && view.title) wm.setTitle(windowId, view.title) }, [windowId, view])

  const page = view && view.markdown !== undefined ? view : null
  const cur = nav.current.current()
  const curU = page ? page.u : null

  /* selection menu */
  const onSelect = () => {
    const sel = window.getSelection()
    if (!sel || sel.isCollapsed || !sel.rangeCount || !bodyRef.current) return setMenu(null)
    const range = sel.getRangeAt(0)
    if (!bodyRef.current.contains(range.commonAncestorContainer)) return setMenu(null)
    const text = cleanSelection(sel.toString())
    if (!selectionMenuItems(text).length) return setMenu(null)
    const b = bodyRef.current.getBoundingClientRect(), r = range.getBoundingClientRect()
    setMenu({ text, x: clampX(r.left - b.left, 130, bodyRef.current.clientWidth), y: r.bottom - b.top + bodyRef.current.scrollTop + 6 })
  }
  const pickMenu = (id) => {
    const text = menu.text
    setMenu(null)
    if (id === 'explore') wm.open('reader', { props: { start: { concept: text, via: 'typed' } }, w: 720, h: 640 })
    else {
      setTab('ask'); setQuestion(askPrefill(text))
      setTimeout(() => { const i = askRef.current && askRef.current.querySelector('input'); if (i) i.focus() }, 0)
    }
  }

  const makePage = () => {
    if (!curU) return
    wm.open('reader', { props: { start: { concept: pageConcept(page.title) || curU, via: 'typed', source_url: curU } }, w: 720, h: 640 })
  }

  /* Ask: stream an answer about this page */
  const runAsk = async (e) => {
    e.preventDefault()
    if (!curU || !question.trim() || ask.running) return
    stopAsk()
    const ac = new AbortController(); askAbort.current = ac
    const live = () => askAbort.current === ac
    setAsk({ running: true, text: '', error: '' })
    try {
      await api.browseAsk(curU, question.trim(), ({ event, data }) => {
        if (!live()) return
        if (event === 'delta') setAsk(a => ({ ...a, text: a.text + (data.text || '') }))
        else if (event === 'error') setAsk(a => ({ ...a, error: data.message || 'The answer could not be written.' }))
      }, ac.signal)
    } catch (err) {
      if (err.name === 'AbortError' || !live()) return
      setAsk(a => ({ ...a, error: err.message || 'The answer could not be written.' }))
    }
    if (live()) { askAbort.current = null; setAsk(a => ({ ...a, running: false })) }
  }

  /* Click through: Primer follows links toward the goal; each step moves this window */
  const runGo = async (e) => {
    e.preventDefault()
    if (!curU || !goal.trim() || go.running) return
    stopGo()
    const ac = new AbortController(); goAbort.current = ac
    const live = () => goAbort.current === ac
    setGo({ running: true, steps: [], answer: '', note: '', error: '', waiting: false })
    try {
      await api.browseAgent(curU, goal.trim(), ({ event, data }) => {
        if (!live()) return
        if (event === 'step') {
          setGo(g => ({ ...g, waiting: false, steps: [...g.steps, data] }))
          { const u = stepTarget(data); if (u) load(u, 'push', true) }
        } else if (event === 'answer') setGo(g => ({ ...g, answer: data.text || '' }))
        else if (event === 'waiting') setGo(g => ({ ...g, waiting: true }))
        else if (event === 'done') setGo(g => ({ ...g, note: data.found === false && !g.answer ? 'I did not find it within the steps allowed.' : '' }))
        else if (event === 'error') setGo(g => ({ ...g, error: data.message || 'Click through stopped.' }))
      }, ac.signal)
    } catch (err) {
      if (err.name === 'AbortError' || !live()) return
      setGo(g => ({ ...g, error: err.message || 'Click through stopped.' }))
    }
    if (live()) { goAbort.current = null; setGo(g => ({ ...g, running: false, waiting: false })) }
  }
  const stopClick = () => { stopGo(); setGo(g => ({ ...g, running: false, waiting: false, note: 'Stopped.' })) }

  const ba = st && st.browse_available
  const unavailable = ba && !ba.ok
  const origin = page && safeHref(page.origin_href)

  return (
    <div className="bw">
      <form className="bw-bar" onSubmit={submit}>
        <Button variant="ghost" size="sm" icon="back" aria-label="Back" title="Back"
                disabled={!nav.current.canBack()} onClick={back} />
        <Button variant="ghost" size="sm" icon="forward" aria-label="Forward" title="Forward"
                disabled={!nav.current.canForward()} onClick={forward} />
        <Button variant="ghost" size="sm" icon="reload" aria-label="Reload" title="Reload"
                disabled={!cur || loading} onClick={reload} />
        <Input className="bw-addr" value={addr} onChange={e => setAddr(e.target.value)} autoComplete="off" spellCheck="false"
               placeholder="Type an address or some words to look up" aria-label="Address" />
        {page ? <Chip tone={page.where === 'reference' ? 'accent' : 'neutral'}>{page.where === 'reference' ? 'offline' : 'web'}</Chip> : null}
        {origin ? <a className="bw-orig" href={origin} target="_blank" rel="noopener noreferrer" title="Open the original in your own browser">Original</a> : null}
      </form>

      {unavailable ? <div className="bw-note" role="status">{ba.reason}</div> : null}

      <div className="bw-main">
        <div className="bw-content" ref={bodyRef} onMouseUp={onSelect} onScroll={() => menu && setMenu(null)}>
          {loading && !view ? <State kind="loading" title="Opening…" /> : null}
          {error ? <State kind="error" title="Couldn’t open that">{error}</State> : null}
          {page ? (
            <article className={'bw-article' + (loading ? ' bw-dim' : '')}>
              <Markdown source={page.markdown} onLink={follow} />
            </article>
          ) : null}
          {view && view.results ? (
            <div className={'bw-results' + (loading ? ' bw-dim' : '')}>
              <h2>{view.results.length ? `Results for “${view.search}” (${whereName(view.where)})` : `Nothing found for “${view.search}” (${whereName(view.where)})`}</h2>
              <ul>
                {view.results.map(r => (
                  <li key={r.url}>
                    <a href={r.url} onClick={e => { e.preventDefault(); if (e.ctrlKey || e.metaKey) openBrowser(r.url, { newWindow: true }); else load(r.url) }}>{r.title || r.url}</a>
                    {r.snippet ? <p>{r.snippet}</p> : null}
                  </li>
                ))}
              </ul>
            </div>
          ) : null}
          {!view && !loading && !error ? <div className="bw-empty">Type an address, or some words to look up.</div> : null}
          {menu ? (
            <div className="bw-menu" role="menu" aria-label="Selection" style={{ left: menu.x, top: menu.y }}
                 onMouseDown={e => e.preventDefault()}>
              {selectionMenuItems(menu.text).map(i => (
                <Button key={i.id} size="sm" role="menuitem" onClick={() => pickMenu(i.id)}>{i.label}</Button>
              ))}
            </div>
          ) : null}
        </div>

        <aside className="bw-side" aria-label="Ask and click through">
          <div className="bw-tabs" role="tablist">
            <button type="button" role="tab" aria-selected={tab === 'ask'} className={tab === 'ask' ? 'on' : ''} onClick={() => setTab('ask')}>Ask</button>
            <button type="button" role="tab" aria-selected={tab === 'go'} className={tab === 'go' ? 'on' : ''} onClick={() => setTab('go')}>Click through</button>
          </div>
          {tab === 'ask' ? (
            <div className="bw-pane">
              <form onSubmit={runAsk} className="bw-form" ref={askRef}>
                <Input value={question} onChange={e => setQuestion(e.target.value)} disabled={!page}
                       placeholder="Ask about this page…" aria-label="Question about this page" autoComplete="off" />
                {ask.running
                  ? <Button size="sm" onClick={() => { stopAsk(); setAsk(a => ({ ...a, running: false })) }} type="button">Stop</Button>
                  : <Button size="sm" variant="primary" type="submit" disabled={!page || !question.trim()}>Ask</Button>}
              </form>
              {ask.text ? <div className="bw-answer"><Markdown source={ask.text} /></div> : null}
              {ask.running && !ask.text ? <Chip busy tone="accent">reading…</Chip> : null}
              {ask.error ? <div className="bw-err" role="alert">{ask.error}</div> : null}
            </div>
          ) : (
            <div className="bw-pane">
              <form onSubmit={runGo} className="bw-form">
                <Input value={goal} onChange={e => setGoal(e.target.value)} disabled={!page || go.running}
                       placeholder="What should I find?" aria-label="Goal" autoComplete="off" />
                {go.running
                  ? <Button size="sm" onClick={stopClick} type="button">Stop</Button>
                  : <Button size="sm" variant="primary" type="submit" disabled={!page || !goal.trim()}>Start</Button>}
              </form>
              {go.steps.length ? (
                <ol className="bw-steps">
                  {go.steps.map(s => (
                    <li key={s.n}>
                      <div className="bw-step-t">{s.title || s.url}</div>
                      {stepLink(s) ? <div className="bw-step-a">followed “{stepLink(s)}”</div> : null}
                      {s.why ? <div className="bw-step-w">{s.why}</div> : null}
                    </li>
                  ))}
                </ol>
              ) : null}
              {go.running ? <Chip busy tone="accent">{go.waiting ? 'waiting for the storyteller…' : 'looking…'}</Chip> : null}
              {go.answer ? <div className="bw-answer"><Markdown source={go.answer} /></div> : null}
              {go.note ? <div className="bw-note-s">{go.note}</div> : null}
              {go.error ? <div className="bw-err" role="alert">{go.error}</div> : null}
            </div>
          )}
          <div className="bw-foot">
            <Button size="sm" disabled={!page} onClick={makePage}
                    title="Write a Primer page from this page alone">Make a page</Button>
          </div>
        </aside>
      </div>
    </div>
  )
}
