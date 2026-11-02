/* VideoWindow — one video job, the Image window's twin. It opens the moment a video is asked for
 * (a Video button, typed text or Animate), shows live progress and the preview frame while the
 * job runs, then the clip with its prompt, model, settings, length and seed. Again / Retry /
 * Edit & run / another model each start a NEW Video window; an Animate job's runs animate the
 * same picture again. Props: { windowId, jobId, subject? }. */
import { useEffect, useRef, useState } from 'react'
import * as api from '../api.js'
import * as wm from '../windowManager.js'
import { openVideoWindow } from '../openImage.js'
import { Button, Chip, Input, Select, TextArea } from '../kit/parts.jsx'
import { pollJob, previewUrl, settingsSummary } from './imageJob.js'
import { againBody, describeVideo, videoLength, videoModelChoices } from './videoJob.js'
import { Words } from './ImageWindow.jsx'
import './images.css'

/* a whole number from a field, or undefined when it isn't one */
const MAX_FRAMES = 241
const whole = (s) => (/^\d+$/.test(String(s).trim()) ? Number(s) : undefined)

export default function VideoWindow({ windowId, jobId, subject = '' }) {
  const [job, setJob] = useState(null)
  const [pollErr, setPollErr] = useState('')
  const [lib, setLib] = useState({ models: [], families: {} })
  const [modelsErr, setModelsErr] = useState('')
  const [gone, setGone] = useState(false)
  const [prompt, setPrompt] = useState('')
  const [frames, setFrames] = useState('')
  const [fps, setFps] = useState('')
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
      if (!editedRef.current) {                       // never overwrite the user's edits
        if (j.prompt) setPrompt(j.prompt)
        if (j.settings?.frames) setFrames(String(j.settings.frames))
        if (j.settings?.fps) setFps(String(j.settings.fps))
      }
    }, { signal: ac.signal, onError: (e) => {
        if (e.status === 404) setGone(true)
        else setPollErr(e.message || 'Could not reach Primer.')
      } })
    return () => ac.abort()
  }, [jobId])

  useEffect(() => {
    let live = true
    api.imageModels().then(d => { if (live) setLib({ models: d.models || [], families: d.families || {} }) })
      .catch((e) => { if (live) setModelsErr(e.message || 'Primer did not answer.') })
    return () => { live = false }
  }, [])

  useEffect(() => {
    const t = job?.phrase || subject || (job?.prompt ? job.prompt.split(/\s+/).slice(0, 5).join(' ') : '')
    if (t) wm.setTitle(windowId, `Video: ${t}`)
  }, [windowId, subject, job?.phrase, job?.prompt])

  const info = describeVideo(job || { status: 'prompting' })
  const running = !!job && !info.done
  const animating = !!job?.settings?.animate_from
  const usable = videoModelChoices(lib.models, lib.families, { animating, current: job?.model_id })
  const model = lib.models.find(m => m.id === job?.model_id)

  // start a new job from this one's inputs and open its own window
  const spawn = async (extra) => {
    setBusy(true); setErr('')
    try {
      const m = await api.createImage(againBody(job, subject || job.phrase || '', extra))
      openVideoWindow(m.id, subject)
    } catch (e) { setErr(e.message || 'Could not start the video.') }
    setBusy(false)
  }
  const again = () => spawn({})
  const nFrames = whole(frames), nFps = whole(fps)
  const settingsOk = nFrames !== undefined && nFrames >= 1 && nFrames <= MAX_FRAMES
    && nFps !== undefined && nFps >= 1 && nFps <= 60
  const editRun = () => {
    const base = againBody(job).overrides
    spawn({ prompt: prompt.trim(), overrides: { ...base, frames: nFrames, fps: nFps } })
  }
  // another model brings its own settings (sizes, frames and steps differ by family); the prompt stays
  const withModel = () => spawn({ model_id: other, overrides: { seed: -1 } })
  const cancel = async () => {
    setErr('')
    try { await api.cancelImage(jobId) } catch (e) { setErr(e.message || 'Could not cancel.') }
  }
  const read = (word) => wm.open('reader', {
    props: { start: { concept: word, via: 'typed', ...(job?.node_id ? { from_id: job.node_id } : {}) } },
    w: 720, h: 640,
  })
  const edit = (set) => (e) => { set(e.target.value); setEdited(true) }

  const preview = job?.status === 'generating' ? previewUrl(job.preview_b64) : ''
  const fileUrl = job?.status === 'done' ? api.videoUrl(job.file) : ''
  const noModels = modelsErr ? `Could not load models: ${modelsErr}` : !usable.length ? 'No other usable video models' : ''
  const length = job ? videoLength(job) : ''
  // why a button is off, as its tooltip (Animate and Video say theirs the same way)
  const notYet = busy ? 'Starting a video…' : !info.done ? 'Wait until this video has finished' : ''
  const againWhy = notYet
  const editWhy = notYet || (!edited ? 'Change the prompt, frames or fps first'
    : !prompt.trim() ? 'Write a prompt first' : !settingsOk ? 'Frames must be 1 to 241 and fps 1 to 60' : '')
  const otherWhy = notYet || noModels || (!other ? 'Choose another model first' : '')

  return (
    <div className="im">
      <div className="im-stage">
        {fileUrl
          ? <video className="im-pic" src={fileUrl} autoPlay loop muted controls playsInline
                   aria-label={subject || job.phrase || job.prompt || 'Generated video'} />
          : preview
            ? <><img className="im-pic" src={preview} alt="Preview of the video being made" />
                <span className="im-tag"><Chip tone="accent" busy>preview</Chip></span></>
            : <div className="im-ph">{gone ? 'This video no longer exists.' : info.failed ? 'No video was made.' : job?.status === 'done' ? 'The video file could not be shown.' : info.done ? 'No video.' : 'The video will appear here.'}</div>}
      </div>

      <div className="im-status" role="status">
        <div className={'im-label' + (info.failed ? ' im-label-err' : '')}>{job ? info.label : 'Starting…'}</div>
        {running ? (
          <div className="im-bar" role="progressbar" aria-valuemin={0} aria-valuemax={100} aria-valuenow={info.pct}>
            <div className="im-bar-fill" style={{ width: `${info.pct}%` }} />
          </div>
        ) : null}
        {gone ? <div className="im-err">This video no longer exists</div> : null}
        {pollErr ? <div className="im-err">{pollErr}</div> : null}
      </div>

      {job ? (
        <div className="im-panel">
          <div>
            <h3 className="im-h">Prompt</h3>
            <TextArea className="im-prompt" aria-label="Prompt" rows={4} value={prompt}
                      placeholder={job.status === 'prompting' ? 'Primer is writing the prompt…' : ''}
                      onChange={edit(setPrompt)} />
            {prompt ? <Words text={prompt} onWord={read} /> : null}
          </div>

          <div className="im-meta">
            <div><b>Model</b> {model?.name || job.model_id}</div>
            {animating ? <div><b>Start</b> animates a picture</div> : null}
            {settingsSummary(job.settings) ? <div><b>Settings</b> {settingsSummary(job.settings)}</div> : null}
            {length ? <div><b>Length</b> {length}</div> : null}
            {job.settings && job.settings.seed !== undefined ? <div><b>Seed</b> <span className="im-mono">{job.settings.seed}</span></div> : null}
          </div>

          <div className="im-len">
            <label>Frames <Input className="im-num" inputMode="numeric" aria-label="Frames" value={frames}
                                 disabled={busy} onChange={edit(setFrames)} /></label>
            <label>fps <Input className="im-num" inputMode="numeric" aria-label="Frames per second" value={fps}
                              disabled={busy} onChange={edit(setFps)} /></label>
            {edited && !settingsOk ? <span className="im-err">Frames must be a whole number from 1 up to 241 frames; fps 1 to 60</span> : null}
          </div>

          <div className="im-actions">
            {running ? <Button size="sm" onClick={cancel}>Cancel</Button> : null}
            {info.canRetry ? <Button variant="primary" size="sm" disabled={busy} onClick={again}>Retry</Button> : null}
            <Button size="sm" disabled={!!againWhy} title={againWhy || 'Make another video with a new seed'} onClick={again}>Again</Button>
            <Button size="sm" disabled={!!editWhy} title={editWhy || 'Make a video with these changes'} onClick={editRun}>Edit &amp; run</Button>
            <Select aria-label="Use another model" value={other} disabled={busy || !info.done || !usable.length} title={noModels || undefined}
                    onChange={(e) => setOther(e.target.value)}>
              <option value="">Another model…</option>
              {usable.map(m => <option key={m.id} value={m.id}>{m.name}</option>)}
            </Select>
            <Button size="sm" disabled={!!otherWhy} title={otherWhy || 'Make this video with the chosen model'} onClick={withModel}>Run with it</Button>
          </div>
          {noModels ? <div className="im-meta">{noModels}</div> : null}
          {err ? <div className="im-err">{err}</div> : null}
        </div>
      ) : null}
    </div>
  )
}
