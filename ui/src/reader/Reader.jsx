/* Reader — one window onto the book. It streams a model-written page, renders it from
 * Markdown with the model's highlighted phrases, keeps its own back/forward history, and
 * always shows the prompt box. Props: { windowId, start? (an open-page body), onPick? }. */
import { useCallback, useEffect, useRef, useState } from 'react'
import * as api from '../api.js'
import { mediaUrl, videoUrl } from '../api.js'
import { MEDIA_EVENT, OPEN_PLACE_EVENT, openDiagramWindow, openImageWindow, openMapWindow, openStoryWindow, openTimelineWindow, openVideoWindow } from '../openImage.js'
import * as wm from '../windowManager.js'
import { Button, Chip, Input, State } from '../kit/parts.jsx'
import { Icon } from '../kit/icons.jsx'
import BranchFork from '../fork/BranchFork.jsx'
import { Markdown } from './markdown.jsx'
import { createHistory } from './history.js'
import { routePrompt, imageButton, imageChecking, imageCheckFailed, mapButton, typedMapBody, mapChecking, mapCheckFailed, thumbState, storyButton, storyChecking, storyCheckFailed,
         timelineButton, timelineChecking, timelineCheckFailed, diagramButton, diagramChecking, diagramCheckFailed } from './imageRoute.js'
import { videoButton, videoChecking, videoCheckFailed } from '../apps/videoJob.js'
import { safeHref } from './citations.js'
import { openBrowser } from '../openBrowser.js'
import { browseAddressFor, kiwixHref } from '../apps/browserNav.js'
import { badgeLabel, rewriteOptions } from './sourceMode.js'
import { retryBody } from './retry.js'
import { streamEndError } from './streamEnd.js'
import { dwellTracker, testMeButton } from '../apps/readerModel.js'
import TestMe from './TestMe.jsx'
import { announceTale } from '../apps/tale.js'
import './reader.css'

const EMPTY = { nodeId: null, title: '', markdown: '', n: 0, depth: 'normal', sourceMode: '', sources: [], grounding: '', note: '',
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
  // this page's images, videos, maps, stories, timelines and diagrams
  const [media, setMedia] = useState({ node: null, items: [], maps: [], stories: [], timelines: [], diagrams: [] })
  const [imgBtn, setImgBtn] = useState(imageChecking)
  const [mapBtn, setMapBtn] = useState(mapChecking)
  const [vidBtn, setVidBtn] = useState(videoChecking)
  const [storyBtn, setStoryBtn] = useState(storyChecking)
  const [timelineBtn, setTimelineBtn] = useState(timelineChecking)
  const [diagramBtn, setDiagramBtn] = useState(diagramChecking)
  const [imgErr, setImgErr] = useState('')
  const [errTitle, setErrTitle] = useState('No image')
  const [mediaErr, setMediaErr] = useState('')
  const [browseWeb, setBrowseWeb] = useState(false)  // whether the Browser can open web pages (else only kiwix:)
  const [browseOk, setBrowseOk] = useState(false)     // whether source links can open in the Browser
  const [status, setStatus] = useState(null)          // the last /v1/status (Test me reads the writing model)
  const [testing, setTesting] = useState(null)        // the node Test me is open for

  const stop = () => { if (abort.current) abort.current.abort(); abort.current = null }
  useEffect(() => stop, [])

  /* Stream a page into this window. mode: 'push' adds a history entry, 'replace' swaps the
   * current one (Deeper, Simpler, Regenerate, Retry). Opening a page aborts the one in
   * flight; the server still finishes and saves it. */
  const run = async (body, mode = 'push', after) => {
    stop()
    const ac = new AbortController()
    abort.current = ac
    const live = () => abort.current === ac
    setView({ ...EMPTY, nodeId: body.node_id || null,
              title: mode === 'replace' ? hist.current.current()?.title || '' : '',
              streaming: true, request: body })
    const ended = { done: false, error: false }
    let nodeId = body.node_id || null  // nodeRef only catches up on render; a cached page ends first
    try {
      await api.openPage(body, ({ event, data }) => {
        if (!live()) return
        if (event === 'done' || event === 'error') ended[event] = true
        if (event === 'node') {
          nodeId = data.id
          const entry = { nodeId: data.id, title: data.title }
          if (mode === 'replace' || hist.current.current()?.nodeId === data.id) hist.current.replace(entry)
          else hist.current.push(entry)
          setView(v => ({ ...v, nodeId: data.id, title: data.title, sourceMode: data.source_mode || v.sourceMode }))
          tick(x => x + 1)
        } else if (event === 'grounding') {
          setView(v => ({ ...v, grounding: data.status || '' }))
        } else if (event === 'sources') {
          setView(v => ({ ...v, sources: data.sources || [], grounding: '' }))
        } else if (event === 'waiting') {
          setView(v => (v.markdown ? v : { ...v, waiting: data.reason || 'paused for a picture or video' }))
        } else if (event === 'delta') {
          setView(v => ({ ...v, waiting: '', markdown: v.markdown + data.text }))
        } else if (event === 'done') {
          setView(v => ({ ...v, n: data.n, depth: data.depth, sourceMode: data.source_mode, note: data.note || '', grounding: '' }))
          announceTale()   // a page read: the Tale's waiting pages (and the dock's mark) change
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
    if (live()) setView(v => ({ ...v, streaming: false, waiting: '', grounding: '' }))
    // the stream's sources carry no links; reload them from the saved page
    if (live() && ended.done) refreshSources(nodeId, ac)
    if (live() && ended.done && !ended.error && nodeId && after) after(nodeId)   // e.g. a typed map request
  }

  const refreshSources = async (id, ac) => {
    if (!id) return
    try {
      const p = await api.page(id)
      if (abort.current !== ac || p.node.id !== id) return
      setView(v => ({ ...v, sources: p.version.sources || v.sources }))
    } catch { /* keep the unlinked list */ }
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
                sourceMode: p.version.source_mode, sources: p.version.sources || [], phrases: p.highlights || [] })
    } catch (e) {
      if (abort.current !== ac) return
      setView(v => ({ ...v, error: e.message || 'The page could not be loaded.' }))
    }
  }

  const go = (entry) => { tick(x => x + 1); if (entry) show(entry) }
  const back = () => go(hist.current.back())
  const forward = () => go(hist.current.forward())

  /* Image and video requests make one and leave the reader where it is; the rest open a page. */
  const sendPrompt = async (e) => {
    e.preventDefault()
    const typed = text.trim()
    if (!typed) return
    setText('')
    const r = await routePrompt(typed, { nodeId: nodeRef.current })
    if (r.kind === 'image' || r.kind === 'video') makeImage(r.body)
    // a typed map request: the place's page first, then its map once the page is written
    else if (r.kind === 'map') run(r.body, 'push', (id) => makeMap(typedMapBody(id, r.body.concept)))
    // a typed story request: the topic's page first, then its story once the page is written
    else if (r.kind === 'story') run(r.body, 'push', (id) => makeStory(id, 'story', r.body.concept))
    // a typed timeline or diagram request: the subject's page first, then its timeline or diagram
    else if (r.kind === 'timeline') run(r.body, 'push', (id) => makeTimeline(id, r.body.concept))
    else if (r.kind === 'diagram') run(r.body, 'push', (id) => makeDiagram({ node_id: id, subject: r.body.concept }))
    else run(r.body)
  }

  /* Create an image or video job (body.kind video); its window opens only on success, a
   * refusal shows in the reader. */
  const makeImage = async (body) => {
    const video = body.kind === 'video'
    setImgErr('')
    try {
      const m = await api.createImage(body)
      if (video) openVideoWindow(m.id, body.subject)
      else openImageWindow(m.id, body.subject)
    } catch (e) {
      setErrTitle(video ? 'No video' : 'No image')
      setImgErr(e.message || (video ? 'The video could not be started.' : 'The image could not be started.'))
    }
  }

  /* Create a map job (a page, or a phrase on it); the Map window opens only on success. */
  const makingMap = useRef(false)
  const makeMap = async (body) => {
    if (makingMap.current) return              // a double click must not start two jobs
    makingMap.current = true
    setImgErr('')
    try {
      const m = await api.createMap(body)
      openMapWindow(m.id, body.subject, body.node_id)
    } catch (e) {
      setErrTitle('No map')
      setImgErr(e.message || 'The map could not be started.')
    } finally { makingMap.current = false }
  }

  /* Create a story (or Listen) of a page; the Story window opens only on success. */
  const makingStory = useRef(false)
  const makeStory = async (nodeId, kind, subject) => {
    if (makingStory.current) return             // a double click must not start two
    makingStory.current = true
    setImgErr('')
    try {
      const r = await api.createStory({ node_id: nodeId, kind })
      openStoryWindow(r.id, subject, nodeId, kind)
    } catch (e) {
      setErrTitle(kind === 'listen' ? 'No narration' : 'No story')
      setImgErr(e.message || (kind === 'listen' ? 'The narration could not be started.' : 'The story could not be started.'))
    } finally { makingStory.current = false }
  }

  /* Create a timeline of a page; the Timeline window opens only on success. */
  const makingTimeline = useRef(false)
  const makeTimeline = async (nodeId, subject) => {
    if (makingTimeline.current) return          // a double click must not start two
    makingTimeline.current = true
    setImgErr('')
    try {
      const r = await api.createTimeline(nodeId)
      openTimelineWindow(r.id, subject, nodeId)
    } catch (e) {
      setErrTitle('No timeline')
      setImgErr(e.message || 'The timeline could not be started.')
    } finally { makingTimeline.current = false }
  }

  /* Create a diagram of a page, or of a phrase on it ({node_id, phrase?, subject}); the Diagram
   * window opens only on success. */
  const makingDiagram = useRef(false)
  const makeDiagram = async ({ subject, ...body }) => {
    if (makingDiagram.current) return           // a double click must not start two
    makingDiagram.current = true
    setImgErr('')
    try {
      const r = await api.createDiagram(body)
      openDiagramWindow(r.id, subject, body.node_id)
    } catch (e) {
      setErrTitle('No diagram')
      setImgErr(e.message || 'The diagram could not be started.')
    } finally { makingDiagram.current = false }
  }

  const loadMedia = useCallback(async (id) => {
    if (!id) return
    try {
      const p = await api.page(id)
      if (id !== nodeRef.current) return              // the reader moved on: this page's images are stale
      setMedia({ node: id, items: p.media || [], maps: p.maps || [], stories: p.stories || [],
                 timelines: p.timelines || [], diagrams: p.diagrams || [] }); setMediaErr('')
    } catch (e) {
      if (id === nodeRef.current) setMediaErr(`Couldn't refresh this page's images: ${e.message || 'Primer did not answer.'}`)
    }
  }, [])
  // a job started anywhere (an Image or Video window's Again, Animate, …) may belong to this page
  useEffect(() => {
    const onMedia = () => loadMedia(nodeRef.current)
    window.addEventListener(MEDIA_EVENT, onMedia)
    return () => window.removeEventListener(MEDIA_EVENT, onMedia)
  }, [loadMedia])
  const mediaItems = media.node === view.nodeId ? media.items : []
  const mapItems = media.node === view.nodeId ? media.maps : []
  const storyItems = media.node === view.nodeId ? media.stories : []
  const timelineItems = media.node === view.nodeId ? media.timelines : []
  const diagramItems = media.node === view.nodeId ? media.diagrams : []
  const unfinished = mediaItems.concat(mapItems, storyItems, timelineItems, diagramItems).some(m => thumbState(m) === 'busy')
  useEffect(() => { if (view.nodeId && !view.streaming) loadMedia(view.nodeId) }, [view.nodeId, view.streaming, loadMedia])
  useEffect(() => {
    if (!unfinished) return
    const t = setInterval(() => loadMedia(view.nodeId), 3000)
    return () => clearInterval(t)
  }, [unfinished, view.nodeId, loadMedia])
  useEffect(() => { setImgErr(''); setMediaErr('') }, [view.nodeId])

  /* Dwell: the seconds this page is on screen (the tab visible and this window not
   * minimised) go to /v1/reader/dwell every 30 s and on leaving; failures are dropped. */
  const dwell = useRef(null)
  useEffect(() => {
    const d = dwellTracker({ report: api.readerDwell, now: Date.now })
    dwell.current = d
    const seen = () => {
      const w = wm.getWindows().find(x => x.id === windowId)
      d.visible(document.visibilityState === 'visible' && !(w && w.minimized))
    }
    seen()
    const unsub = wm.subscribe(seen)
    document.addEventListener('visibilitychange', seen)
    const bye = () => d.flush()   // the tab may come back from the back/forward cache: the visit goes on
    window.addEventListener('pagehide', bye)
    const t = setInterval(d.tick, 5000)
    return () => {
      clearInterval(t); unsub()
      document.removeEventListener('visibilitychange', seen)
      window.removeEventListener('pagehide', bye)
      d.leave()
    }
  }, [windowId])
  useEffect(() => { if (dwell.current) dwell.current.show(view.nodeId) }, [view.nodeId])
  useEffect(() => { setTesting(null) }, [view.nodeId])
  // Deeper, Simpler, Regenerate or a rewrite: close Test me, so the new version doesn't make
  // new questions by itself (a GPU call nobody asked for)
  useEffect(() => { if (view.streaming) setTesting(null) }, [view.streaming])
  useEffect(() => {
    let live = true
    api.status().then(st => { if (live) { setStatus(st); setBrowseOk(!!(st.browse_available && st.browse_available.ok)); setBrowseWeb(!!(st.browse_available && st.browse_available.web)); setImgBtn(imageButton(st)); setVidBtn(videoButton(st)); setMapBtn(mapButton(st)); setStoryBtn(storyButton(st)); setTimelineBtn(timelineButton(st)); setDiagramBtn(diagramButton(st)) } })
      .catch(e => { if (live) { setImgBtn(imageCheckFailed(e)); setVidBtn(videoCheckFailed(e)); setMapBtn(mapCheckFailed(e)); setStoryBtn(storyCheckFailed(e)); setTimelineBtn(timelineCheckFailed(e)); setDiagramBtn(diagramCheckFailed(e)) } })
    return () => { live = false }
  }, [view.nodeId])

  /* A chosen branch card: open its page here, or in a new Reader. */
  const follow = (card, phrase, { newWindow = false } = {}) => {
    const body = { concept: card.concept, via: card.via, from_id: nodeRef.current, phrase }
    if (newWindow) wm.open('reader', { props: { start: body } })
    else run(body)
  }

  /* A place chosen on a Map window for the page this reader shows: open it here. */
  const runRef = useRef(null)
  runRef.current = run
  const streamingRef = useRef(false)
  streamingRef.current = view.streaming
  useEffect(() => {
    const onPlace = (e) => {
      const d = e.detail
      if (!d || d.handled || !d.body || d.body.from_id !== nodeRef.current || streamingRef.current) return
      d.handled = true
      runRef.current(d.body)
    }
    window.addEventListener(OPEN_PLACE_EVENT, onPlace)
    return () => window.removeEventListener(OPEN_PLACE_EVENT, onPlace)
  }, [])

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
  const forkVideo = () => {
    const phrase = fork.phrase, node = fork.node
    closeFork()
    makeImage({ node_id: node, phrase, subject: phrase, kind: 'video' })
  }
  const forkMap = () => {
    const phrase = fork.phrase, node = fork.node
    closeFork()
    makeMap({ node_id: node, phrase, subject: phrase })
  }
  const forkDiagram = () => {
    const phrase = fork.phrase, node = fork.node
    closeFork()
    makeDiagram({ node_id: node, phrase, subject: phrase })
  }
  const forkStory = () => {
    const phrase = fork.phrase, node = fork.node
    closeFork()
    // the phrase's General page first, then its story once that page is written
    run({ concept: phrase, via: 'general', from_id: node, phrase }, 'push', (id) => makeStory(id, 'story', phrase))
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
  // a rewrite keeps the page's own source mode, so the badge doesn't flip to the default
  const reload = (extra) => run({ node_id: view.nodeId, ...(view.sourceMode ? { source: view.sourceMode } : {}), ...extra }, 'replace')
  const [menu, setMenu] = useState(null)       // the "Rewrite from" options while open, else null
  const openMenu = async () => {
    if (menu) { setMenu(null); return }
    setMenu(rewriteOptions(null))
    try { const opts = rewriteOptions(await api.status()); setMenu(m => (m ? opts : m)) } catch { /* keep the storyteller-only menu */ }
  }
  const rewrite = (mode) => { setMenu(null); run({ node_id: view.nodeId, regenerate: true, source: mode }, 'replace') }
  const badgeRef = useRef(null)
  useEffect(() => {
    if (!menu) return
    const onDown = (e) => { if (badgeRef.current && !badgeRef.current.contains(e.target)) setMenu(null) }
    const items = () => [...badgeRef.current.querySelectorAll('[role=menuitem]:not(:disabled)')]
    const onKey = (e) => {
      if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
        const it = items(); if (!it.length) return
        e.preventDefault()
        const i = it.indexOf(document.activeElement)
        it[(i + (e.key === 'ArrowDown' ? 1 : it.length - 1)) % it.length].focus()
      } else if (e.key === 'Escape') { setMenu(null); badgeRef.current?.querySelector('button')?.focus() }
    }
    document.addEventListener('mousedown', onDown)
    document.addEventListener('keydown', onKey)
    return () => { document.removeEventListener('mousedown', onDown); document.removeEventListener('keydown', onKey) }
  }, [menu])
  useEffect(() => { setMenu(null) }, [view.nodeId])
  // focus the first enabled item whenever the options (re)appear
  useEffect(() => { if (menu) badgeRef.current?.querySelector('[role=menuitem]:not(:disabled)')?.focus() }, [menu])
  /* A plain click on a source opens it in the Browser lens; shift (or ctrl/cmd/middle) keeps
   * the old way, a new browser tab. */
  const openSource = (e, source) => {
    if (!browseOk || e.shiftKey || e.ctrlKey || e.metaKey || e.button !== 0) return
    const u = browseAddressFor(source && source.href)
    if (!u || (!browseWeb && !kiwixHref(u))) return   // web is off: a web source opens in a new tab as before
    e.preventDefault()
    openBrowser(u)
  }
  const hasPage = view.nodeId || view.streaming || view.markdown
  const tmBtn = testMeButton(status, { nodeId: view.nodeId, streaming: view.streaming })

  return (
    <div className="rd">
      <div className="rd-bar">
        <Button variant="ghost" size="sm" icon="back" aria-label="Back" title="Back"
                disabled={!hist.current.canBack()} onClick={back} />
        <Button variant="ghost" size="sm" icon="forward" aria-label="Forward" title="Forward"
                disabled={!hist.current.canForward()} onClick={forward} />
        <div className={'rd-title' + (view.title ? '' : ' rd-title-dim')}>{view.title || 'New page'}</div>
        {view.sourceMode ? (
          <div className="rd-badge" ref={badgeRef}>
            <Chip tone="accent" onClick={view.nodeId && !view.streaming ? openMenu : undefined}
                  title={view.nodeId && !view.streaming ? 'Rewrite from another source' : undefined}>
              {badgeLabel(view.sourceMode, view.sources.length)}
            </Chip>
            {menu ? (
              <div className="rd-menu" role="menu" aria-label="Rewrite from">
                <div className="rd-menu-h">Rewrite from</div>
                {menu.map(o => (
                  <Button key={o.mode} size="sm" role="menuitem" disabled={o.disabled}
                          title={o.disabled ? o.reason : undefined} onClick={() => rewrite(o.mode)}>{o.label}</Button>
                ))}
              </div>
            ) : null}
          </div>
        ) : null}
        {view.n ? <Chip>{`v${view.n}${view.depth !== 'normal' ? ' · ' + view.depth : ''}`}</Chip> : null}
        <div className="rd-actions">
          <Button size="sm" disabled={!view.nodeId || view.streaming || !imgBtn.enabled}
                  title={imgBtn.enabled ? 'Draw a picture of this page' : imgBtn.reason}
                  aria-label={imgBtn.enabled ? 'Image' : `Image (unavailable: ${imgBtn.reason})`}
                  onClick={() => makeImage({ node_id: view.nodeId, subject: view.title })}>Image</Button>
          <Button size="sm" disabled={!view.nodeId || view.streaming || !vidBtn.enabled}
                  title={vidBtn.enabled ? 'Make a short video of this page' : vidBtn.reason}
                  aria-label={vidBtn.enabled ? 'Video' : `Video (unavailable: ${vidBtn.reason})`}
                  onClick={() => makeImage({ node_id: view.nodeId, subject: view.title, kind: 'video' })}>Video</Button>
          <Button size="sm" disabled={!view.nodeId || view.streaming || !mapBtn.enabled}
                  title={mapBtn.enabled ? 'Show the places this page names on a map' : mapBtn.reason}
                  aria-label={mapBtn.enabled ? 'Map' : `Map (unavailable: ${mapBtn.reason})`}
                  onClick={() => makeMap({ node_id: view.nodeId, subject: view.title })}>Map</Button>
          <Button size="sm" disabled={!view.nodeId || view.streaming || !timelineBtn.enabled}
                  title={timelineBtn.enabled ? 'Put the dates this page names on a timeline' : timelineBtn.reason}
                  aria-label={timelineBtn.enabled ? 'Timeline' : `Timeline (unavailable: ${timelineBtn.reason})`}
                  onClick={() => makeTimeline(view.nodeId, view.title)}>Timeline</Button>
          <Button size="sm" disabled={!view.nodeId || view.streaming || !diagramBtn.enabled}
                  title={diagramBtn.enabled ? 'Draw a labelled diagram of how this works' : diagramBtn.reason}
                  aria-label={diagramBtn.enabled ? 'Diagram' : `Diagram (unavailable: ${diagramBtn.reason})`}
                  onClick={() => makeDiagram({ node_id: view.nodeId, subject: view.title })}>Diagram</Button>
          <Button size="sm" disabled={!view.nodeId || view.streaming || !storyBtn.enabled}
                  title={storyBtn.enabled ? 'Read this page aloud' : storyBtn.reason}
                  aria-label={storyBtn.enabled ? 'Listen' : `Listen (unavailable: ${storyBtn.reason})`}
                  onClick={() => makeStory(view.nodeId, 'listen', view.title)}>Listen</Button>
          <Button size="sm" disabled={!view.nodeId || view.streaming || !storyBtn.enabled}
                  title={storyBtn.enabled ? 'Retell this page as an illustrated, narrated story' : storyBtn.reason}
                  aria-label={storyBtn.enabled ? 'Story' : `Story (unavailable: ${storyBtn.reason})`}
                  onClick={() => makeStory(view.nodeId, 'story', view.title)}>Story</Button>
          <Button size="sm" disabled={!tmBtn.enabled} aria-pressed={testing === view.nodeId && !!view.nodeId}
                  title={tmBtn.enabled ? 'Three questions about this page' : tmBtn.reason}
                  aria-label={tmBtn.enabled ? 'Test me' : `Test me (unavailable: ${tmBtn.reason})`}
                  onClick={() => setTesting(t => (t === view.nodeId ? null : view.nodeId))}>Test me</Button>
          <Button size="sm" disabled={!view.nodeId || view.streaming}
                  title="A quest through the pages that led here"
                  onClick={() => wm.open('quest', { props: { fromNodeId: view.nodeId }, single: true, w: 600, h: 720 })}>Quest</Button>
          <Button size="sm" disabled={!view.nodeId || view.streaming} onClick={() => reload({ depth: 'deeper' })}>Deeper</Button>
          <Button size="sm" disabled={!view.nodeId || view.streaming} onClick={() => reload({ depth: 'simpler' })}>Simpler</Button>
          <Button size="sm" disabled={!view.nodeId || view.streaming} onClick={() => reload({ regenerate: true })}>Regenerate</Button>
        </div>
      </div>

      {view.note ? <div className="rd-note" role="status">{view.note}</div> : null}

      <div className="rd-body" ref={bodyRef} onScroll={() => fork && closeFork()}>
        {hasPage ? (
          <div className="rd-article" ref={articleRef} tabIndex={-1} onMouseUp={onSelect}>
            {view.grounding && !view.markdown ? <div className="rd-grounding" role="status">{view.grounding}</div> : null}
            <Markdown source={view.markdown} phrases={view.phrases} sources={view.sources} onPhrase={pick} onSource={openSource} />
            {view.streaming && !view.error ? <div className="rd-writing"><Chip busy tone="accent">{view.waiting ? 'paused for a picture or video' : 'writing…'}</Chip></div> : null}
            {mediaItems.length || mapItems.length || storyItems.length || timelineItems.length || diagramItems.length ? (
              <ul className="rd-strip" aria-label="Images, videos, maps, stories, timelines and diagrams on this page">
                {mediaItems.map(m => {
                  const video = m.kind === 'video', what = video ? 'Video' : 'Image'
                  const st = thumbState(m), url = st !== 'done' ? '' : video ? videoUrl(m.file) : mediaUrl(m.file)
                  const open = video ? openVideoWindow : openImageWindow
                  return (
                    <li key={m.id}>
                      <button type="button" className={`rd-thumb rd-thumb-${st}`}
                              title={m.phrase || m.prompt || what}
                              aria-label={`${what}: ${m.phrase || m.prompt || 'untitled'} (${st === 'busy' ? 'in progress' : st})`}
                              onClick={() => open(m.id, m.phrase || view.title)}>
                        {url && video
                          ? <><video src={`${url}#t=0.1`} preload="metadata" muted playsInline aria-hidden="true" tabIndex={-1} />
                              <span className="rd-thumb-play" aria-hidden="true">{'▶︎'}</span></>
                          : url ? <img src={url} alt="" />
                          : <span className="rd-thumb-glyph" aria-hidden="true">{st === 'failed' ? '✕' : <span className="ui-spin" />}</span>}
                      </button>
                      {m.phrase ? <span className="rd-thumb-cap">{m.phrase}</span> : null}
                    </li>
                  )
                })}
                {mapItems.map(m => {
                  const st = thumbState(m)
                  return (
                    <li key={m.id}>
                      <button type="button" className={`rd-thumb rd-thumb-map rd-thumb-${st}`}
                              title={m.phrase || 'Map of this page'}
                              aria-label={`Map: ${m.phrase || view.title || 'this page'} (${st === 'busy' ? 'in progress' : st})`}
                              onClick={() => openMapWindow(m.id, m.phrase || view.title, m.node_id || view.nodeId)}>
                        <span className="rd-thumb-glyph" aria-hidden="true">
                          {st === 'busy' ? <span className="ui-spin" /> : st === 'failed' ? '✕' : <Icon name="map" size={32} />}
                        </span>
                      </button>
                      <span className="rd-thumb-cap">{m.phrase || 'Map'}</span>
                    </li>
                  )
                })}
                {storyItems.map(m => {
                  const st = thumbState(m), listen = m.kind === 'listen'
                  const name = listen ? 'Listen' : 'Story'
                  return (
                    <li key={m.id}>
                      <button type="button" className={`rd-thumb rd-thumb-map rd-thumb-${st}`}
                              title={m.title || name}
                              aria-label={`${name}: ${m.title || view.title || 'this page'} (${st === 'busy' ? 'in progress' : st})`}
                              onClick={() => openStoryWindow(m.id, m.title || view.title, m.node_id || view.nodeId, m.kind)}>
                        <span className="rd-thumb-glyph" aria-hidden="true">
                          {st === 'busy' ? <span className="ui-spin" /> : st === 'failed' ? '✕' : <Icon name={listen ? 'speaker' : 'story'} size={32} />}
                        </span>
                      </button>
                      <span className="rd-thumb-cap">{listen ? 'Listen' : m.title || 'Story'}</span>
                    </li>
                  )
                })}
                {timelineItems.map(m => {
                  const st = thumbState(m)
                  const n = st === 'done' && typeof m.event_count === 'number' ? m.event_count : null
                  return (
                    <li key={m.id}>
                      <button type="button" className={`rd-thumb rd-thumb-map rd-thumb-${st}`}
                              title={n === null ? 'Timeline of this page' : `Timeline: ${n} ${n === 1 ? 'event' : 'events'}`}
                              aria-label={`Timeline: ${view.title || 'this page'} (${st === 'busy' ? 'in progress' : st})`}
                              onClick={() => openTimelineWindow(m.id, view.title, m.node_id || view.nodeId)}>
                        <span className="rd-thumb-glyph" aria-hidden="true">
                          {st === 'busy' ? <span className="ui-spin" /> : st === 'failed' ? '✕' : <Icon name="timeline" size={32} />}
                        </span>
                      </button>
                      <span className="rd-thumb-cap">Timeline</span>
                    </li>
                  )
                })}
                {diagramItems.map(m => {
                  const st = thumbState(m)
                  return (
                    <li key={m.id}>
                      <button type="button" className={`rd-thumb rd-thumb-map rd-thumb-${st}`}
                              title={m.caption || m.phrase || 'Diagram of this page'}
                              aria-label={`Diagram: ${m.phrase || view.title || 'this page'} (${st === 'busy' ? 'in progress' : st})`}
                              onClick={() => openDiagramWindow(m.id, m.phrase || view.title, m.node_id || view.nodeId)}>
                        <span className="rd-thumb-glyph" aria-hidden="true">
                          {st === 'busy' ? <span className="ui-spin" /> : st === 'failed' ? '✕' : <Icon name="diagram" size={32} />}
                        </span>
                      </button>
                      <span className="rd-thumb-cap">{m.phrase || 'Diagram'}</span>
                    </li>
                  )
                })}
              </ul>
            ) : null}
            {view.sources.length && !view.streaming ? (
              <section className="rd-sources">
                <h3>Sources</h3>
                <ol>
                  {view.sources.map(s => (
                    <li key={s.n} value={s.n}>
                      {safeHref(s.href)
                        ? <a href={safeHref(s.href)} target="_blank" rel="noopener noreferrer"
                           title={browseOk ? 'Open in the Browser (shift-click: a new tab)' : undefined}
                           onClick={e => openSource(e, s)}>{s.title}</a>
                        : <span>{s.title}</span>}
                      {s.where ? <span className="rd-where">{s.where}</span> : null}
                    </li>
                  ))}
                </ol>
              </section>
            ) : null}
            {testing && testing === view.nodeId && !view.streaming
              ? <TestMe key={testing} nodeId={testing} onClose={() => setTesting(null)} /> : null}
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
                        phrase={fork.phrase} onChoose={choose} onAction={act} onImage={forkImage} onVideo={forkVideo} onMap={forkMap} onDiagram={forkDiagram} onStory={forkStory} onClose={closeFork} />
          </div>
        ) : null}
      </div>

      {imgErr ? (
        <div className="rd-imgerr">
          <State kind="error" title={errTitle} action={<Button size="sm" onClick={() => setImgErr('')}>Dismiss</Button>}>{imgErr}</State>
        </div>
      ) : null}

      <form className="rd-prompt" ref={promptRef} onSubmit={sendPrompt}>
        <Input value={text} onChange={e => setText(e.target.value)} placeholder="Ask about anything…"
               aria-label="What do you want to explore?" autoComplete="off" />
      </form>
    </div>
  )
}
