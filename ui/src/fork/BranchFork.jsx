/* BranchFork — curved lines grow from a clicked phrase to "in this context / general /
 * sideways" cards, with Image and Video (each live when available, else disabled with the
 * reason) and Ask about it.
 * Props: { anchor, container, node, phrase, onChoose(card, {newWindow}), onAction(kind), onImage(), onVideo(), onClose }.
 * It is positioned in the coordinates of `container` ({w, h}); its parent places it. */
import { useEffect, useId, useLayoutEffect, useRef, useState } from 'react'
import * as api from '../api.js'
import { layoutFork } from './geometry.js'
import { imageButton, imageChecking, imageCheckFailed } from '../reader/imageRoute.js'
import { videoButton, videoChecking, videoCheckFailed } from '../apps/videoJob.js'
import './fork.css'

const TAGS = { context: 'in this context', general: 'general', sideways: 'sideways' }
const KINDS = ['context', 'general', 'sideways']
const CARD_H = 80

export default function BranchFork({ anchor, container, node, phrase, onChoose, onAction, onImage, onVideo, onClose }) {
  const [img, setImg] = useState(imageChecking)
  const [vid, setVid] = useState(videoChecking)
  const [state, setState] = useState({ cards: null, error: '' })
  const [attempt, setAttempt] = useState(0)
  const root = useRef(null)
  const uid = useId()

  useEffect(() => {
    let live = true
    setState({ cards: null, error: '' })
    api.branches(node, phrase)
      .then(r => { if (live) setState({ cards: r.cards || [], error: '' }) })
      .catch(e => { if (live) setState({ cards: null, error: 'Could not load branches. Check that the Primer is running.' }) })
    return () => { live = false }
  }, [node, phrase, attempt])

  // availability changes (a model added, the GPU busy): read it fresh each time the fork opens
  useEffect(() => {
    let live = true
    api.status().then(st => { if (live) { setImg(imageButton(st)); setVid(videoButton(st)) } })
      .catch(e => { if (live) { setImg(imageCheckFailed(e)); setVid(videoCheckFailed(e)) } })
    return () => { live = false }
  }, [])

  const loading = !state.cards && !state.error
  const empty = !!state.cards && !state.cards.length
  const failed = !!state.error || empty
  const shown = state.cards && state.cards.length ? state.cards : null
  const n = shown ? shown.length : 3
  const cardW = Math.max(120, Math.min(200, container.w - 16))
  const lay = layoutFork(anchor, container, n, { w: cardW, h: CARD_H })
  const kindAt = (i) => (shown ? shown[i].via : KINDS[i])

  /* focus moves into the fork on open, and onto the first card once the skeletons are replaced */
  useLayoutEffect(() => {
    const el = root.current
    const first = el.querySelector('button.fk-card') || el.querySelector('.fk-retry')
    const at = document.activeElement
    if (first && (at === el || !el.contains(at))) first.focus({ preventScroll: true })
    else if (!first && !el.contains(at)) el.focus({ preventScroll: true })
  }, [state])

  /* dismiss: Escape, or a press outside the fork (the article's scroll is the Reader's to watch) */
  useEffect(() => {
    const down = (e) => { if (!root.current.contains(e.target)) onClose() }
    const key = (e) => { if (e.key === 'Escape') { e.preventDefault(); onClose() } }
    document.addEventListener('pointerdown', down, true)
    document.addEventListener('keydown', key)
    return () => { document.removeEventListener('pointerdown', down, true); document.removeEventListener('keydown', key) }
  }, [onClose])

  /* Tab cycles inside the fork */
  const onKeyDown = (e) => {
    if (e.key !== 'Tab') return
    const f = [...root.current.querySelectorAll('button:not([disabled])')]
    e.preventDefault()
    if (!f.length) return
    const i = f.indexOf(document.activeElement)
    f[e.shiftKey ? (i <= 0 ? f.length - 1 : i - 1) : (i + 1) % f.length].focus()
  }

  const choose = (card, newWindow) => onChoose(card, { newWindow })

  return (
    <div className="fk" ref={root} tabIndex={-1} role="dialog" aria-label={`Where next from "${phrase}"`}
         onKeyDown={onKeyDown}>
      <svg className="fk-lines" width={container.w} height={container.h} aria-hidden="true">
        {failed ? null : lay.paths.map((d, i) => (
          <path key={i} d={d} pathLength="1" className={`fk-line fk-line-${kindAt(i)}`}
                style={{ animationDelay: `${i * 70}ms` }} />
        ))}
      </svg>

      {failed ? (
        <div className="fk-error" role="alert"
             style={{ left: lay.cards[0].x, top: lay.cards[0].y, width: Math.min(lay.actions.w, 420) }}>
          <span>{state.error || 'No branches found for this phrase.'}</span>
          <button type="button" className="fk-retry" onClick={() => setAttempt(a => a + 1)}>Retry</button>
        </div>
      ) : lay.cards.map((pos, i) => {
        const card = shown && shown[i]
        const kind = kindAt(i)
        const style = { left: pos.x, top: pos.y, width: cardW, height: CARD_H, animationDelay: `${i * 70}ms` }
        if (!card) return (
          <div key={i} className={`fk-card fk-skel fk-${kind}`} style={style} aria-hidden="true">
            <i className="fk-bar fk-bar-tag" /><i className="fk-bar fk-bar-title" /><i className="fk-bar fk-bar-blurb" />
          </div>
        )
        return (
          <button key={i} type="button" className={`fk-card fk-${kind}`} style={style}
                  onClick={e => choose(card, e.ctrlKey || e.metaKey)}
                  onKeyDown={e => { if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) { e.preventDefault(); choose(card, true) } }}
                  onAuxClick={e => { if (e.button === 1) { e.preventDefault(); choose(card, true) } }}>
            <span className="fk-tag">{TAGS[kind] || kind}</span>
            <b className="fk-title">{card.title}</b>
            <span className="fk-blurb">{card.blurb}</span>
          </button>
        )
      })}

      {failed ? null : (
        <div className="fk-actions" style={{ left: lay.actions.x, top: lay.actions.y, width: lay.actions.w }}>
          {img.enabled ? null : <span id={uid + "-image"} className="fk-sr">{img.reason}</span>}
          {vid.enabled ? null : <span id={uid + "-video"} className="fk-sr">{vid.reason}</span>}
          {img.enabled
            ? <button type="button" className="fk-chip" onClick={() => onImage()}>Image</button>
            : <button type="button" className="fk-chip" aria-disabled="true" aria-describedby={uid + "-image"}
                      aria-busy={img.checking || undefined} title={img.reason} onClick={e => e.preventDefault()}>Image</button>}
          {vid.enabled
            ? <button type="button" className="fk-chip" onClick={() => onVideo()}>Video</button>
            : <button type="button" className="fk-chip" aria-disabled="true" aria-describedby={uid + "-video"}
                      aria-busy={vid.checking || undefined} title={vid.reason} onClick={e => e.preventDefault()}>Video</button>}
          <button type="button" className="fk-chip" onClick={() => onAction('ask')}>Ask about it</button>
        </div>
      )}
      {loading ? <span className="fk-sr" role="status">Finding where this could lead…</span> : null}
    </div>
  )
}
