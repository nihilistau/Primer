/* StoryWindow — a page retold as scenes (Story) or read aloud (Listen), narrated. It opens the
 * moment the story is asked for, shows a status line while Primer writes, draws and narrates,
 * and shows every scene's text from the start (muted until it is narrated). Play needs a click;
 * it starts scene 1 as soon as its narration exists, and later scenes continue automatically as
 * they finish (at the end of a scene whose successor isn't ready it waits and says so). The sentence being spoken
 * is lit in the text; highlighted phrases open the phrase's page. Again tells it anew.
 * A Tale's chapter (kind "chapter") is headed "Chapter N of <tale>": `chapter` {n, taleTitle} comes
 * from the Tale window or the story's own `tale` field {id, title, n} (which wins); without that
 * field (older servers) the current tale is asked once which chapter it is (else the heading is
 * "Chapter" and the story's title).
 * Props: { windowId, storyId, subject?, nodeId?, kind?, chapter? }. */
import { useEffect, useMemo, useReducer, useRef, useState } from 'react'
import { Markdown } from '../reader/markdown.jsx'
import * as api from '../api.js'
import * as wm from '../windowManager.js'
import { Button } from '../kit/parts.jsx'
import { Icon } from '../kit/icons.jsx'
import { openPlace, MEDIA_EVENT } from '../openImage.js'
import { describeJob } from './imageJob.js'
import { chunkAt, initial, next, readyList, waitingFor } from './storyPlayer.js'
import { isEnded, pollStory, sceneSegments, storyStatusLabel, waitingLabel } from './storyJob.js'
import { chapterHeading, findChapter, storyChapter } from './tale.js'
import './apps.css'

const SILENT_MS = 1500        // how long a scene with no audio (nothing to say) is shown before the next

export default function StoryWindow({ windowId, storyId, subject = '', nodeId = '', kind = 'story', chapter = null }) {
  const [curId, setCurId] = useState(storyId)
  const [nonce, setNonce] = useState(0)          // bumps on Again, so the poll restarts even on the same id
  const [story, setStory] = useState(null)
  const [pollErr, setPollErr] = useState('')
  const [gone, setGone] = useState(false)
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState('')
  const [audioErr, setAudioErr] = useState('')
  const [pos, setPos] = useState({ scene: 0, t: 0 })   // the audio element's position, and the scene it is in
  const [player, dispatch] = useReducer(next, undefined, () => initial([]))
  const audio = useRef(null)
  const sceneRef = useRef(null)
  useEffect(() => () => { if (audio.current) audio.current.pause() }, [])     // closing the window stops the voice

  // poll this story until it and its pictures are settled
  useEffect(() => {
    const ac = new AbortController()
    setGone(false)
    pollStory(curId, (s) => { setStory(s); setPollErr('') }, { signal: ac.signal, onError: (e) => {
      if (e.status === 404) setGone(true)
      else setPollErr(e.message || 'Could not reach Primer.')
    } })
    return () => ac.abort()
  }, [curId, nonce])

  // tell the player which scenes can play (the string key keeps it to real changes)
  const scenes = (story && story.scenes) || []
  const readyKey = readyList(scenes).map(Number).join('')
  useEffect(() => { dispatch({ type: 'ready', ready: readyKey.split('').map(c => c === '1') }) }, [readyKey])

  const listen = (story ? story.kind : kind) === 'listen'
  const sc = scenes[player.scene] || null
  const src = sc ? api.audioUrl(sc.audio) : ''
  const silent = !!sc && sc.status === 'done' && !sc.audio

  // run the audio element as the player says
  useEffect(() => {
    const a = audio.current
    if (!a) return
    setAudioErr('')
    if (!player.playing || !src) { a.pause(); return }
    a.play().catch((e) => {
      if (e && e.name === 'AbortError') return        // a newer play() or a src change took over
      setAudioErr(e && e.message ? `The audio could not be played: ${e.message}` : 'The audio could not be played.')
      dispatch({ type: 'pause' })
    })
  }, [player.playing, player.scene, src])

  // a scene with nothing to say passes after a moment
  useEffect(() => {
    if (!player.playing || !silent) return undefined
    const t = setTimeout(() => dispatch({ type: 'ended' }), SILENT_MS)
    return () => clearTimeout(t)
  }, [player.playing, player.scene, silent])

  useEffect(() => { setPos({ scene: player.scene, t: 0 }) }, [player.scene, curId, nonce])
  useEffect(() => { if (sceneRef.current && sceneRef.current.scrollIntoView) sceneRef.current.scrollIntoView({ block: 'nearest' }) }, [player.scene])
  const secs = pos.scene === player.scene ? pos.t : 0       // a position from the previous scene is not this one's

  const highlights = story && story.highlights
  const phrases = useMemo(() => highlights || [], [highlights])
  // every scene's text: the current one carries the lit chunk, the others are plain
  const segsOf = (g, i) => sceneSegments(g.spoken, g.chunks, i === player.scene && (player.playing || secs > 0) ? chunkAt(g.chunks, secs) : -1, phrases)

  const openPhrase = (phrase, e) => {
    const from = (story && story.node_id) || nodeId
    openPlace({ concept: phrase, via: 'general', ...(from ? { from_id: from } : {}), phrase },
              { newWindow: e.shiftKey || e.ctrlKey || e.metaKey })
  }

  const title = (story && story.title) || subject
  const isChapter = (story ? story.kind : kind) === 'chapter'
  const [chapterInfo, setChapterInfo] = useState(chapter)
  const own = storyChapter(story)     // the story's own `tale` field, when the server sends it
  const hasOwn = !!story && story.tale !== undefined
  useEffect(() => {
    // older servers: ask the current tale once which chapter this is
    if (!isChapter || chapterInfo || !story || hasOwn) return undefined
    let live = true
    api.tales().then(s => { const f = findChapter(s, curId); if (live && f) setChapterInfo(f) }).catch(() => {})
    return () => { live = false }
  }, [isChapter, curId, !!story, hasOwn])   // eslint-disable-line react-hooks/exhaustive-deps
  const heading = isChapter ? chapterHeading(own || chapterInfo, title) : ''
  useEffect(() => {
    wm.setTitle(windowId, isChapter ? heading
      : title ? `${listen ? 'Listen' : 'Story'}: ${title}` : (listen ? 'Listen' : 'Story'))
  }, [windowId, title, listen, isChapter, heading])

  const again = async () => {
    setBusy(true); setErr('')
    try {
      const r = await api.storyAgain(curId)
      if (audio.current) audio.current.pause()
      dispatch({ type: 'reset' }); setStory(null)
      setCurId(r.id || curId); setNonce(n => n + 1)
      window.dispatchEvent(new Event(MEDIA_EVENT))
    } catch (e) { setErr(e.message || 'Could not start the story again.') }
    setBusy(false)
  }

  const seek = (i) => {
    if (i === player.scene && audio.current) audio.current.currentTime = 0
    dispatch({ type: 'seek', scene: i })
  }
  const toggle = () => dispatch({ type: player.playing || player.waiting ? 'pause' : 'play' })

  const ended = isEnded(story)
  const failed = story && story.status === 'failed'
  const label = gone ? 'This story no longer exists.' : storyStatusLabel(story)
  const pic = sc && sc.picture
  const picUrl = pic && pic.status === 'done' ? api.mediaUrl(pic.file) : ''
  const picState = pic && !picUrl && pic.status !== 'failed' && pic.status !== 'cancelled' ? describeJob(pic).label : ''
  const canPlay = !gone && (player.ready.some(Boolean) || !ended)
  const waiting = player.waiting ? waitingLabel(waitingFor(player), story) : ''
  const lastReady = player.ready.length ? player.ready.lastIndexOf(true) : -1

  return (
    <div className="st">
      {isChapter ? <h2 className="st-chapter">{heading}</h2> : null}
      <div className="st-top">
        <div className={'st-label' + (failed || gone ? ' st-label-err' : '')} role="status">
          {ended || gone ? null : <span className="ui-spin" aria-hidden="true" />} {label}
        </div>
        <Button size="sm" disabled={busy || !ended} onClick={again}>Again</Button>
      </div>

      {!listen ? (
        <div className="st-stage">
          {picUrl ? <img className="st-pic" src={picUrl} alt={sc && sc.title ? `Picture for ${sc.title}` : ''} />
            : <div className="st-ph">
                <b>{sc && sc.title ? sc.title : title || ''}</b>
                {picState ? <span>{picState}</span> : null}
              </div>}
        </div>
      ) : null}

      <div className="st-text" aria-live="off">
        {scenes.length ? scenes.map((g, i) => {
          const on = i === player.scene
          const segs = segsOf(g, i)
          const mark = g.status === 'narrating' ? 'narrating…' : g.status === 'waiting' ? 'waiting' : ''
          return (
            <section key={g.idx != null ? g.idx : i} ref={on ? sceneRef : undefined}
                     className={'st-part' + (on ? ' st-part-on' : '') + (g.status === 'done' ? '' : ' st-part-muted')}>
              {!listen && g.title ? <h3 className="st-h">{g.title}{mark ? <span className="st-mark">{mark}</span> : null}</h3>
                : mark ? <div className="st-mark">{mark}</div> : null}
              {g.status === 'failed' ? <p className="st-fail">This part could not be narrated{g.error ? `: ${g.error}` : ''}</p> : null}
              {segs.length ? (
                <p className="st-spoken">
                  {segs.map((x, k) => x.phrase ? (
                    <span key={k} className={`rd-phrase st-${x.part}`} role="button" tabIndex={0}
                          onClick={(e) => openPhrase(x.phrase, e)}
                          onAuxClick={(e) => { if (e.button === 1) { e.preventDefault(); openPhrase(x.phrase, { ...e, ctrlKey: true }) } }}
                          onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); openPhrase(x.phrase, e) } }}>{x.text}</span>
                  ) : <span key={k} className={`st-${x.part}`}>{x.text}</span>)}
                </p>
              ) : g.text ? <Markdown source={g.text} /> : null}
            </section>
          )
        }) : <p className="st-dim">{failed ? 'Nothing was written.' : 'The text appears here as it is written.'}</p>}
      </div>

      <div className="st-controls">
        <Button size="sm" icon="back" variant="ghost" aria-label="Previous scene" title="Previous scene"
                disabled={player.scene <= 0} onClick={() => seek(player.scene - 1)} />
        <Button size="sm" variant="primary" disabled={!canPlay} onClick={toggle}
                aria-label={player.playing || player.waiting ? 'Pause' : 'Play'}>
          {player.playing || player.waiting ? 'Pause' : player.finished ? 'Play again' : 'Play'}
        </Button>
        <Button size="sm" icon="forward" variant="ghost" aria-label="Next scene" title="Next scene"
                disabled={player.scene >= scenes.length - 1} onClick={() => seek(player.scene + 1)} />
        <span className="st-wait" role="status">{waiting}</span>
        {story && story.voice ? <span className="st-voice"><Icon name="speaker" size={14} /> {story.voice}</span> : null}
      </div>

      {!listen && scenes.length > 1 ? (
        <ol className="st-strip" aria-label="Scenes">
          {scenes.map((s, i) => (
            <li key={s.idx != null ? s.idx : i}>
              <button type="button" className={'st-scene' + (i === player.scene ? ' st-scene-on' : '') + (s.status === 'failed' ? ' st-scene-bad' : '')}
                      aria-current={i === player.scene ? 'true' : undefined}
                      title={s.status === 'done' ? s.title : `${s.title} (${s.status === 'failed' ? 'failed' : 'not narrated yet'})`}
                      onClick={() => seek(i)}>
                <span className="st-scene-n">{i + 1}</span>
                <span className="st-scene-t">{s.title}</span>
                {s.status === 'narrating' ? <span className="ui-spin" aria-hidden="true" /> : null}
              </button>
            </li>
          ))}
        </ol>
      ) : null}

      {/* one audio element, its source changing per scene */}
      <audio ref={audio} src={src || undefined} preload="auto"
             onTimeUpdate={(e) => setPos({ scene: player.scene, t: e.currentTarget.currentTime })}
             onEnded={() => dispatch({ type: 'ended' })}
             onError={() => { if (src) { setAudioErr('The narration could not be loaded.'); dispatch({ type: 'pause' }) } }} />
      {audioErr ? <div className="st-err">{audioErr}</div> : null}
      {pollErr ? <div className="st-err">{pollErr}</div> : null}
      {err ? <div className="st-err">{err}</div> : null}
      {failed && lastReady >= 0 ? <div className="st-dim st-note">The scenes that were narrated can still be played.</div> : null}
    </div>
  )
}
