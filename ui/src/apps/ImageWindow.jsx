/* ImageWindow — one image job. It opens the moment an image is asked for, shows live
 * progress (and sd-server's preview frame) while the job runs, then the picture with its
 * prompt, model, settings and seed. Again / Edit & run / another model each start a NEW
 * Image window and leave this one as it is; Animate starts a Video window that animates this
 * picture. Props: { windowId, jobId, subject? }. */
import { useEffect, useRef, useState } from 'react'
import * as api from '../api.js'
import * as wm from '../windowManager.js'
import { Button, Chip, Select, TextArea } from '../kit/parts.jsx'
import { openImageWindow, openVideoWindow } from '../openImage.js'
import { describeJob, pollJob, previewUrl, settingsSummary } from './imageJob.js'
import { animateButton } from './videoJob.js'
import './images.css'

/* the prompt as clickable words: punctuation stays out of the concept it opens */
export function Words({ text, onWord }) {
  const parts = text.split(/(\s+)/)
  return (
    <p className="im-words" aria-label="Prompt words">
      {parts.map((p, i) => {
        if (!p.trim()) return p
        const word = p.replace(/^[^\p{L}\p{N}]+|[^\p{L}\p{N}]+$/gu, '')
        return word
          ? <button key={i} type="button" className="im-word" title={`Read about "${word}"`} onClick={() => onWord(word)}>{p}</button>
          : p
      })}
    </p>
  )
}

export default function ImageWindow({ windowId, jobId, subject = '' }) {
  const [job, setJob] = useState(null)
  const [pollErr, setPollErr] = useState('')
  const [models, setModels] = useState([])
  const [modelsErr, setModelsErr] = useState('')
  const [anim, setAnim] = useState({ enabled: false, reason: 'Checking whether Animate is available…' })
  const [gone, setGone] = useState(false)
  const [prompt, setPrompt] = useState('')
  const [edited, setEdited] = useState(false)
  const [other, setOther] = useState('')
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState('')
  const editedRef = useRef(false)
  editedRef.current = edited

  // poll this job to its end
  useEffect(() => {
    const ac = new AbortController()
    pollJob(jobId, (j) => {
      setJob(j); setPollErr('')
      if (!editedRef.current && j.prompt) setPrompt(j.prompt)   // never overwrite the user's edits
    }, { signal: ac.signal, onError: (e) => {
        if (e.status === 404) setGone(true)
        else setPollErr(e.message || 'Could not reach Primer.')
      } })
    return () => ac.abort()
  }, [jobId])

  useEffect(() => {
    let live = true
    api.imageModels().then(d => { if (live) { setModels(d.models || []); setAnim(animateButton(d)) } })
      .catch((e) => {
        if (!live) return
        setModelsErr(e.message || 'Primer did not answer.')
        setAnim({ enabled: false, reason: `Couldn't check whether Animate is available: ${e.message || 'Primer did not answer.'}` })
      })
    return () => { live = false }
  }, [])

  useEffect(() => {
    const t = subject || job?.phrase || (job?.prompt ? job.prompt.split(/\s+/).slice(0, 5).join(' ') : '')
    if (t) wm.setTitle(windowId, t)
  }, [windowId, subject, job?.phrase, job?.prompt])

  const info = describeJob(job || { status: 'prompting' })
  const running = !!job && !info.done
  const usable = models.filter(m => m.kind !== 'video' && !(m.problems && m.problems.length) && m.id !== job?.model_id)
  const model = models.find(m => m.id === job?.model_id)

  // start a new job from this one's inputs and open its own window
  const spawn = async (extra) => {
    setBusy(true); setErr('')
    try {
      const m = await api.createImage({
        node_id: job.node_id, phrase: job.phrase || '', subject,
        model_id: job.model_id, overrides: { ...(job.settings || {}), seed: -1 }, prompt: job.prompt || undefined,
        ...extra,
      })
      openImageWindow(m.id, subject)
    } catch (e) { setErr(e.message || 'Could not start the image.') }
    setBusy(false)
  }
  const again = () => spawn({})
  const editRun = () => spawn({ prompt: prompt.trim() })
  // another model brings its own settings (sizes and steps differ by family); the prompt stays
  const withModel = () => spawn({ model_id: other, overrides: { seed: -1 } })
  // a short video that starts from this picture, on the model the server picks for Animate
  const animate = async () => {
    setBusy(true); setErr('')
    const about = subject || job.phrase || (job.prompt || '').slice(0, 200)
    try {
      const m = await api.createImage({ node_id: job.node_id, phrase: job.phrase || '', subject: about,
                                        kind: 'video', animate_from: jobId })
      openVideoWindow(m.id, subject || job.phrase || '')
    } catch (e) { setErr(e.message || 'Could not start the video.') }
    setBusy(false)
  }
  const cancel = async () => {
    setErr('')
    try { await api.cancelImage(jobId) } catch (e) { setErr(e.message || 'Could not cancel.') }
  }
  const read = (word) => wm.open('reader', {
    props: { start: { concept: word, via: 'typed', ...(job?.node_id ? { from_id: job.node_id } : {}) } },
    w: 720, h: 640,
  })

  const preview = job?.status === 'generating' ? previewUrl(job.preview_b64) : ''
  const fileUrl = job?.status === 'done' ? api.mediaUrl(job.file) : ''
  const noModels = modelsErr ? `Could not load models: ${modelsErr}` : !usable.length ? 'No other usable models' : ''

  return (
    <div className="im">
      <div className="im-stage">
        {fileUrl
          ? <img className="im-pic" src={fileUrl} alt={subject || job.prompt || 'Generated image'} />
          : preview
            ? <><img className="im-pic" src={preview} alt="Preview of the image being made" />
                <span className="im-tag"><Chip tone="accent" busy>preview</Chip></span></>
            : <div className="im-ph">{gone ? 'This image no longer exists.' : info.failed ? 'No image was made.' : job?.status === 'done' ? 'The image file could not be shown.' : info.done ? 'No image.' : 'The picture will appear here.'}</div>}
      </div>

      <div className="im-status" role="status">
        <div className={'im-label' + (info.failed ? ' im-label-err' : '')}>{job ? info.label : 'Starting…'}</div>
        {running ? (
          <div className="im-bar" role="progressbar" aria-valuemin={0} aria-valuemax={100} aria-valuenow={info.pct}>
            <div className="im-bar-fill" style={{ width: `${info.pct}%` }} />
          </div>
        ) : null}
        {gone ? <div className="im-err">This image no longer exists</div> : null}
        {pollErr ? <div className="im-err">{pollErr}</div> : null}
      </div>

      {job ? (
        <div className="im-panel">
          <div>
            <h3 className="im-h">Prompt</h3>
            <TextArea className="im-prompt" aria-label="Prompt" rows={4} value={prompt}
                      placeholder={job.status === 'prompting' ? 'Primer is writing the prompt…' : ''}
                      onChange={(e) => { setPrompt(e.target.value); setEdited(true) }} />
            {prompt ? <Words text={prompt} onWord={read} /> : null}
          </div>

          <div className="im-meta">
            <div><b>Model</b> {model?.name || job.model_id}</div>
            {settingsSummary(job.settings) ? <div><b>Settings</b> {settingsSummary(job.settings)}</div> : null}
            {job.settings && job.settings.seed !== undefined ? <div><b>Seed</b> <span className="im-mono">{job.settings.seed}</span></div> : null}
          </div>

          <div className="im-actions">
            {running ? <Button size="sm" onClick={cancel}>Cancel</Button> : null}
            {info.canRetry ? <Button variant="primary" size="sm" disabled={busy} onClick={again}>Retry</Button> : null}
            <Button size="sm" disabled={busy || !info.done} onClick={again}>Again</Button>
            <Button size="sm" disabled={busy || !info.done || !edited || !prompt.trim()} onClick={editRun}>Edit &amp; run</Button>
            <Select aria-label="Use another model" value={other} disabled={busy || !info.done || !usable.length} title={noModels || undefined}
                    onChange={(e) => setOther(e.target.value)}>
              <option value="">Another model…</option>
              {usable.map(m => <option key={m.id} value={m.id}>{m.name}</option>)}
            </Select>
            <Button size="sm" disabled={busy || !info.done || !other} onClick={withModel}>Run with it</Button>
            <Button size="sm" disabled={busy || job.status !== 'done' || !anim.enabled}
                    title={anim.enabled ? 'Make a short video that starts from this picture' : anim.reason}
                    aria-label={anim.enabled ? 'Animate' : `Animate (unavailable: ${anim.reason})`}
                    onClick={animate}>Animate</Button>
          </div>
          {noModels ? <div className="im-meta">{noModels}</div> : null}
          {err ? <div className="im-err">{err}</div> : null}
        </div>
      ) : null}
    </div>
  )
}
