/* Reader — one window onto the book. It streams a model-written page, renders it from
 * Markdown with the model's highlighted phrases, keeps its own back/forward history, and
 * always shows the prompt box. Props: { windowId, start? (an open-page body), onPick? }. */
import { useCallback, useEffect, useRef, useState } from 'react'
import * as api from '../api.js'
import { mediaUrl } from '../api.js'
import { openImageWindow } from '../openImage.js'
import * as wm from '../windowManager.js'
import { Button, Chip, Input, State } from '../kit/parts.jsx'
import BranchFork from '../fork/BranchFork.jsx'
import { Markdown } from './markdown.jsx'
import { createHistory } from './history.js'
import { routePrompt, imageButton, imageChecking, imageCheckFailed, thumbState } from './imageRoute.js'
import { retryBody } from './retry.js'
import { streamEndError } from './streamEnd.js'
import './reader.css'

const EMPTY = { nodeId: null, title: '', markdown: '', n: 0, depth: 'normal', sourceMode: '',
                phrases: [], streaming: false, error: '', request: null, waiting: '' }

/* The pick seam. A phrase click or a 1-80 character selection calls
 * onPick({phrase, rect, newWindow}, follow); `follow(card, phrase, {newWindow})` is how a
 * chosen card opens its page. With no onPick the Reader shows its own branch fork;
 * logPick is a stand-in for tests and tinkering. */
export const logPick = (pick) => console.log('[reader] pick', pick.phrase, pick)

export default function Reader({ windowId, start, onPick }) {
  const [view, setView] = useState(EMPTY)
  const [text, setText] = useState('')
  const [, tick] = useState(0)                 // the history is a ref; this redraws its buttons
  const hist = useRef(createHistory())
  const abort = useRef(null)
  const articleRef = useRef(null)
  const nodeRef = useRef(null)
  const bodyRef = useRef(null)
  const promptRef = useRef(null)
  const [fork, setFork] = useState(null)       // {phrase, anchor, container, node, key} while the fork is open
  nodeRef.current = view.nodeId
  const [media, setMedia] = useState({ node: null, items: [] })   // this page's images
  const [imgBtn, setImgBtn] = useState(imageChecking)
  const [imgErr, setImgErr] = useState('')
  const [mediaErr, setMediaErr] = useState('')

  const stop = () => { if (abort.current) abort.current.abort(); abort.current = null }
  useEffect(() => stop, [])

  /* Stream a page into this window. mode: 'push' adds a history entry, 'replace' swaps the
   * current one (Deeper, Simpler, Regenerate, Retry). Opening a page aborts the one in
   * flight; the server still finishes and saves it. */
  const run = async (body, mode = 'push') => {
    stop()
    const ac = new AbortController()
    abort.current = ac
    const live = () => abort.current === ac
    setView({ ...EMPTY, nodeId: body.node_id || null,
              title: mode === 'replace' ? hist.current.current()?.title || '' : '',
              streaming: true, request: body })
    const ended = { done: false, error: false }
    try {
      await api.openPage(body, ({ event, data }) => {
        if (!live()) return
        if (event === 'done' || event === 'error') ended[event] = true
        if (event === 'node') {
          const entry = { nodeId: data.id, title: data.title }
          if (mode === 'replace' || hist.current.current()?.nodeId === data.id) hist.current.replace(entry)
          else hist.current.push(entry)
          setView(v => ({ ...v, nodeId: data.id, title: data.title, sourceMode: data.source_mode || v.sourceMode }))
          tick(x => x + 1)
        } else if (event === 'waiting') {
          setView(v => (v.markdown ? v : { ...v, waiting: data.reason || 'paused for an image' }))
        } else if (event === 'delta') {
          setView(v => ({ ...v, waiting: '', markdown: v.markdown + data.text }))
        } else if (event === 'done') {
          setView(v => ({ ...v, n: data.n, depth: data.depth, sourceMode: data.source_mode }))
        } else if (event === 'highlights') {
          setView(v => ({ ...v, phrases: data.phrases || [] }))
        } else if (event === 'error') {
          setView(v => ({ ...v, error: data.message || 'The page could not be written.' }))
        }
      }, ac.signal)
      const cut = live() && streamEndError(ended)   // Primer stopped mid-page: offer Retry
      if (cut) setView(v => ({ ...v, error: cut }))
    } catch (e) {
      if (e.name === 'AbortError' || !live()) return
      setView(v => ({ ...v, error: e.message || 'The page could not be written.' }))
    }
    if (live()) setView(v => ({ ...v, streaming: false, waiting: '' }))
  }

  /* Show an existing page (back/forward): loaded from storage, never regenerated. */
  const show = async (entry) => {
    stop()
    const ac = new AbortController()
    abort.current = ac
    setView({ ...EMPTY, nodeId: entry.nodeId, title: entry.title, request: { node_id: entry.nodeId } })
    try {
      const p = await api.page(entry.nodeId)
      if (abort.current !== ac) return
      setView({ ...EMPTY, nodeId: p.node.id, title: p.node.title, request: { node_id: entry.nodeId },
                markdown: p.version.markdown, n: p.version.n, depth: p.version.depth,
                sourceMode: p.version.source_mode, phrases: p.highlights || [] })
    } catch (e) {
      if (abort.current !== ac) return
      setView(v => ({ ...v, error: e.message || 'The page could not be loaded.' }))
    }
  }

  const go = (entry) => { tick(x => x + 1); if (entry) show(entry) }
  const back = () => go(hist.current.back())
  const forward = () => go(hist.current.forward())

  /* Image requests make an image and leave the reader where it is; the rest open a page. */
  const sendPrompt = async (e) => {
    e.preventDefault()
    const typed = text.trim()
    if (!typed) return
    setText('')
    const r = await routePrompt(typed, { nodeId: nodeRef.current })
    if (r.kind === 'image') makeImage(r.body)
    else run(r.body)
  }

  /* Create an image job; its window opens only on success, a refusal shows in the reader. */
  const makeImage = async (body) => {
    setImgErr('')
    try {
      const m = await api.createImage(body)
      openImageWindow(m.id, body.subject)
      loadMedia(nodeRef.current)
    } catch (e) {
      setImgErr(e.message || 'The image could not be started.')
    }
  }

  const loadMedia = useCallback(async (id) => {
    if (!id) return
    try {
      const p = await api.page(id)
      if (id !== nodeRef.current) return              // the reader moved on: this page's images are stale
      setMedia({ node: id, items: p.media || [] }); setMediaErr('')
    } catch (e) {
      if (id === nodeRef.current) setMediaErr(`Couldn't refresh this page's images: ${e.message || 'Primer did not answer.'}`)
    }
  }, [])
  const mediaItems = media.node === view.nodeId ? media.items : []
  const unfinished = mediaItems.some(m => thumbState(m) === 'busy')
  useEffect(() => { if (view.nodeId && !view.streaming) loadMedia(view.nodeId) }, [view.nodeId, view.streaming, loadMedia])
  useEffect(() => {
    if (!unfinished) return
    const t = setInterval(() => loadMedia(view.nodeId), 3000)
    return () => clearInterval(t)
  }, [unfinished, view.nodeId, loadMedia])
  useEffect(() => { setImgErr(''); setMediaErr('') }, [view.nodeId])
  useEffect(() => {
    let live = true
    api.status().then(st => { if (live) setImgBtn(imageButton(st)) })
      .catch(e => { if (live) setImgBtn(imageCheckFailed(e)) })
    return () => { live = false }
  }, [view.nodeId])

  /* A chosen branch card: open its page here, or in a new Reader. */
  const follow = (card, phrase, { newWindow = false } = {}) => {
    const body = { concept: card.concept, via: card.via, from_id: nodeRef.current, phrase }
    if (newWindow) wm.open('reader', { props: { start: body } })
    else run(body)
  }

  /* The fork lives in the reader body's visible area (an overlay at the current scroll
   * offset), so the viewport rect becomes body-relative; scrolling the article closes it. */
  const showFork = (p) => {
    const body = bodyRef.current
    if (!body || !nodeRef.current) return
    const b = body.getBoundingClientRect()
    setFork({ phrase: p.phrase, node: nodeRef.current, key: Date.now(),
              anchor: { x: p.rect.left - b.left, y: p.rect.top - b.top, w: p.rect.width, h: p.rect.height },
              container: { w: Math.floor(body.clientWidth), h: Math.floor(body.clientHeight) },
              at: { x: body.scrollLeft, y: body.scrollTop } })
  }
  const pick = (p) => (onPick || showFork)(p, follow)

  /* Closing returns focus to the article, unless the user already moved it elsewhere. */
  const closeFork = useCallback(() => {
    setFork(null)
    const at = document.activeElement
    if (!at || at === document.body || at.closest('.fk')) articleRef.current?.focus({ preventScroll: true })
  }, [])
  const choose = (card, opts) => { const phrase = fork.phrase; closeFork(); follow(card, phrase, opts) }
  const forkImage = () => {
    const phrase = fork.phrase, node = fork.node
    closeFork()
    makeImage({ node_id: node, phrase, subject: phrase })
  }
  const act = (kind) => {
    if (kind !== 'ask') return
    const phrase = fork.phrase
    setFork(null)
    setText(`Tell me about "${phrase}"`)
    promptRef.current?.querySelector('input')?.focus()
  }
  useEffect(() => { setFork(null) }, [view.nodeId])        // a different page: the old fork means nothing

  const onSelect = (e) => {
    const sel = window.getSelection()
    if (!sel || sel.isCollapsed || !sel.rangeCount || !articleRef.current) return
    const range = sel.getRangeAt(0)
    if (!articleRef.current.contains(range.commonAncestorContainer)) return
    const el = range.commonAncestorContainer
    // a selection inside one highlighted phrase is the double-click that also fired the phrase pick
    if ((el.nodeType === 1 ? el : el.parentElement)?.closest('.rd-phrase')) return
    const phrase = sel.toString().replace(/\s+/g, ' ').trim()
    if (phrase.length < 1 || phrase.length > 80) return
    pick({ phrase, rect: range.getBoundingClientRect(), newWindow: e.ctrlKey || e.metaKey })
  }

  useEffect(() => { if (start) run(start) }, [])             // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => { if (view.title) wm.setTitle(windowId, view.title) }, [windowId, view.title])

  const retry = () => run(retryBody(view.request, view.nodeId),
                          view.nodeId ? 'replace' : 'push')
  const reload = (extra) => run({ node_id: view.nodeId, ...extra }, 'replace')
  const hasPage = view.nodeId || view.streaming || view.markdown

  return (
    <div className="rd">
      <div className="rd-bar">
        <Button variant="ghost" size="sm" icon="back" aria-label="Back" title="Back"
                disabled={!hist.current.canBack()} onClick={back} />
        <Button variant="ghost" size="sm" icon="forward" aria-label="Forward" title="Forward"
                disabled={!hist.current.canForward()} onClick={forward} />
        <div className={'rd-title' + (view.title ? '' : ' rd-title-dim')}>{view.title || 'New page'}</div>
        {view.sourceMode ? <Chip tone="accent">{view.sourceMode}</Chip> : null}
        {view.n ? <Chip>{`v${view.n}${view.depth !== 'normal' ? ' · ' + view.depth : ''}`}</Chip> : null}
        <div className="rd-actions">
          <Button size="sm" disabled={!view.nodeId || view.streaming || !imgBtn.enabled}
                  title={imgBtn.enabled ? 'Draw a picture of this page' : imgBtn.reason}
                  aria-label={imgBtn.enabled ? 'Image' : `Image (unavailable: ${imgBtn.reason})`}
                  onClick={() => makeImage({ node_id: view.nodeId, subject: view.title })}>Image</Button>
          <Button size="sm" disabled={!view.nodeId || view.streaming} onClick={() => reload({ depth: 'deeper' })}>Deeper</Button>
          <Button size="sm" disabled={!view.nodeId || view.streaming} onClick={() => reload({ depth: 'simpler' })}>Simpler</Button>
          <Button size="sm" disabled={!view.nodeId || view.streaming} onClick={() => reload({ regenerate: true })}>Regenerate</Button>
        </div>
      </div>

      <div className="rd-body" ref={bodyRef} onScroll={() => fork && closeFork()}>
        {hasPage ? (
          <div className="rd-article" ref={articleRef} tabIndex={-1} onMouseUp={onSelect}>
            <Markdown source={view.markdown} phrases={view.phrases} onPhrase={pick} />
            {view.streaming && !view.error ? <div className="rd-writing"><Chip busy tone="accent">{view.waiting ? 'paused for an image' : 'writing…'}</Chip></div> : null}
            {mediaItems.length ? (
              <ul className="rd-strip" aria-label="Images on this page">
                {mediaItems.map(m => {
                  const st = thumbState(m), url = st === 'done' ? mediaUrl(m.file) : ''
                  return (
                    <li key={m.id}>
                      <button type="button" className={`rd-thumb rd-thumb-${st}`}
                              title={m.phrase || m.prompt || 'Image'}
                              aria-label={`Image: ${m.phrase || m.prompt || 'untitled'} (${st === 'busy' ? 'in progress' : st})`}
                              onClick={() => openImageWindow(m.id, m.phrase || view.title)}>
                        {url ? <img src={url} alt="" /> : <span className="rd-thumb-glyph" aria-hidden="true">{st === 'failed' ? '✕' : <span className="ui-spin" />}</span>}
                      </button>
                      {m.phrase ? <span className="rd-thumb-cap">{m.phrase}</span> : null}
                    </li>
                  )
                })}
              </ul>
            ) : null}
            {mediaErr ? <div className="rd-media-err" role="status">{mediaErr}</div> : null}
            {view.error ? (
              <State kind="error" title="This page did not finish"
                     action={<Button variant="primary" size="sm" onClick={retry}>Retry</Button>}>
                {view.error}
              </State>
            ) : null}
          </div>
        ) : <div className="rd-empty">What do you want to explore?</div>}
        {fork ? (
          <div className="rd-fork" style={{ left: fork.at.x, top: fork.at.y }}>
            <BranchFork key={fork.key} anchor={fork.anchor} container={fork.container} node={fork.node}
                        phrase={fork.phrase} onChoose={choose} onAction={act} onImage={forkImage} onClose={closeFork} />
          </div>
        ) : null}
      </div>

      {imgErr ? (
        <div className="rd-imgerr">
          <State kind="error" title="No image" action={<Button size="sm" onClick={() => setImgErr('')}>Dismiss</Button>}>{imgErr}</State>
        </div>
      ) : null}

      <form className="rd-prompt" ref={promptRef} onSubmit={sendPrompt}>
        <Input value={text} onChange={e => setText(e.target.value)} placeholder="Ask about anything…"
               aria-label="What do you want to explore?" autoComplete="off" />
      </form>
    </div>
  )
}
