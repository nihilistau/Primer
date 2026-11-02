/* Image models — the pictures' models: which are ready, which is the default, edit one's files and
 * settings, or scan a folder for models to add. The server is authoritative; its 400 text shows
 * beside the form. */
import { useEffect, useRef, useState } from 'react'
import * as api from '../api.js'
import { Button, Chip, Input, Select, State } from '../kit/parts.jsx'
import { Icon } from '../kit/icons.jsx'
import { Body, usePoll } from './panel.jsx'
import { blankForm, formFromModel, isDirty, modelFromForm, settingFields } from './imageModelsForm.js'
import './apps.css'

const firstProblem = (m) => (m.problems && m.problems.length ? m.problems[0] : null)

const sameFiles = (a, b) => JSON.stringify(Object.entries(a || {}).sort()) === JSON.stringify(Object.entries(b || {}).sort())

function Editor({ model, family, familyId, isDefault, onChanged, onGone, onDirty }) {
  const [form, setForm] = useState(() => formFromModel(model, family))
  const base = useRef(form) // what "unsaved" is measured against
  const [errors, setErrors] = useState([])
  const [busy, setBusy] = useState(false)
  const isNew = !model.id
  const dirty = isDirty(form, base.current)
  useEffect(() => { onDirty(dirty) }, [dirty])
  useEffect(() => () => onDirty(false), [])
  // a new model switching family keeps its name and the slot paths the new family also has
  const famRef = useRef(familyId)
  useEffect(() => {
    if (!isNew || famRef.current === familyId) return
    famRef.current = familyId
    const next = formFromModel({ name: form.name, family: familyId, files: form.files, settings: {} }, family)
    base.current = blankForm(familyId, family) // kept edits stay unsaved
    setForm(next)
  }, [familyId])

  const run = async (fn) => {
    setBusy(true); setErrors([])
    try { await fn() } catch (e) { setErrors([String(e.message || e)]) }
    setBusy(false)
  }
  const setFile = (k, v) => setForm(f => ({ ...f, files: { ...f.files, [k]: v } }))
  const setSetting = (k, v) => setForm(f => ({ ...f, settings: { ...f.settings, [k]: v } }))

  const save = () => run(async () => {
    const { model: m, errors: errs } = modelFromForm(form, family)
    if (errs.length) { setErrors(errs); return }
    const saved = await api.saveImageModel(m)
    // show what the server kept (it rounds sizes and clamps), not what was typed
    const next = formFromModel(saved, family)
    base.current = next
    setForm(next)
    onChanged(saved)
  })
  const problems = model.problems || []
  const slotNote = (slot) => {
    if (isNew || !(model.files || {})[slot.name]) return null
    return problems.some(p => p.startsWith(slot.name + ':'))
      ? <span className="im-note-bad">not found or not usable</span> : <span className="im-note-ok">found</span>
  }

  return (
    <div className="imm-edit">
      <label className="imm-field"><span className="imm-label">Name</span>
        <Input value={form.name} onChange={(e) => setForm(f => ({ ...f, name: e.target.value }))} /></label>
      <div className="imm-note">{family.label}</div>
      <h4 className="en-h">Files</h4>
      {family.slots.map(s => (
        <label key={s.name} className="imm-field">
          <span className="imm-label">{s.name}{s.required ? '' : ' (optional)'} {slotNote(s)}</span>
          <Input value={form.files[s.name]} placeholder="absolute path" spellCheck={false}
                 onChange={(e) => setFile(s.name, e.target.value)} />
        </label>
      ))}
      <h4 className="en-h">Settings</h4>
      <div className="imm-grid">
        {settingFields(family).map(f => (
          <label key={f.key} className={'imm-field' + (f.kind === 'bool' ? ' imm-check' : '')} title={f.hint}>
            {f.kind === 'bool'
              ? <><input type="checkbox" checked={!!form.settings[f.key]} onChange={(e) => setSetting(f.key, e.target.checked)} />
                  <span className="imm-label">{f.label}</span></>
              : <><span className="imm-label">{f.label}</span>
                  {f.kind === 'sampler'
                    ? <Select value={form.settings[f.key]} onChange={(e) => setSetting(f.key, e.target.value)}>
                        {f.options.map(o => <option key={o} value={o}>{o}</option>)}</Select>
                    : <Input value={form.settings[f.key]} onChange={(e) => setSetting(f.key, e.target.value)} />}
                  <span className="imm-hint">{f.hint}</span></>}
          </label>
        ))}
      </div>
      {!isNew && problems.length ? <ul className="imm-problems">{problems.map((p, i) => <li key={i}>{p}</li>)}</ul> : null}
      {errors.length ? <ul className="en-err imm-errors" role="alert">{errors.map((e, i) => <li key={i}>{e}</li>)}</ul> : null}
      <div className="imm-actions">
        <Button variant="primary" disabled={busy} onClick={save}>Save</Button>
        {!isNew ? <Button disabled={busy || isDefault}
                          onClick={() => run(async () => { await api.setDefaultImageModel(model.id); onChanged() })}>
          Make default</Button> : null}
        {!isNew ? <Button variant="danger" disabled={busy}
                          onClick={() => { if (window.confirm(`Delete ${model.name}?`)) run(async () => { await api.deleteImageModel(model.id); onGone() }) }}>
          Delete</Button> : null}
      </div>
    </div>
  )
}

function Scan({ families, models, onAdded }) {
  const [folder, setFolder] = useState('')
  const [result, setResult] = useState(null)
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState('')
  const [added, setAdded] = useState({})
  const [adding, setAdding] = useState({})

  const scan = async () => {
    setBusy(true); setErr(''); setResult(null); setAdded({})
    try { setResult(await api.scanFolder(folder.trim())) } catch (e) { setErr(String(e.message || e)) }
    setBusy(false)
  }
  const add = async (p, i) => {
    if (adding[i]) return
    setErr(''); setAdding(a => ({ ...a, [i]: true }))
    const fam = families[p.family]
    try {
      await api.saveImageModel({ name: p.name, family: p.family, files: p.files, settings: { ...((fam && fam.defaults) || {}) } })
      setAdded(a => ({ ...a, [i]: true })); onAdded()
    } catch (e) { setErr(String(e.message || e)) }
    setAdding(a => ({ ...a, [i]: false }))
  }

  return (
    <section className="en-sec">
      <h3 className="en-h">Scan a folder</h3>
      <div className="imm-scan">
        <Input aria-label="Folder to scan" value={folder} placeholder="absolute folder path" spellCheck={false}
               onChange={(e) => setFolder(e.target.value)}
               onKeyDown={(e) => { if (e.key === 'Enter' && folder.trim() && !busy) scan() }} />
        <Button disabled={busy || !folder.trim()} onClick={scan}>Scan</Button>
      </div>
      {err ? <div className="en-err" role="alert">{err}</div> : null}
      {result && result.truncated ? <div className="en-note">Stopped after 2,000 files — pick a narrower folder</div> : null}
      {result && !result.proposals.length ? <div className="en-note">No models found in that folder.</div> : null}
      {result ? result.proposals.map((p, i) => {
        const have = models.some(m => m.family === p.family && sameFiles(m.files, p.files))
        return (
        <div key={i} className="imm-prop">
          <div className="imm-prop-top">
            <span className="imm-name">{p.name}</span>
            <Chip>{(families[p.family] || {}).label || p.family}</Chip>
            {p.missing.length ? <Chip tone="warn">incomplete</Chip> : <Chip tone="ok" dot>complete</Chip>}
            <span className="en-actions">
              <Button size="sm" disabled={have || !!added[i] || !!adding[i]} onClick={() => add(p, i)}>
                {have || added[i] ? 'Already added' : 'Add'}</Button>
            </span>
          </div>
          {p.missing.length ? <div className="en-reason">missing: {p.missing.join(', ')}</div> : null}
          {(p.notes || []).map((n, j) => <div key={j} className="en-note">{n}</div>)}
        </div>
        )
      }) : null}
    </section>
  )
}

export default function ImageModels() {
  const poll = usePoll(api.imageModels, 10000) // the banner goes stale after a tier switch
  const [sel, setSel] = useState(null) // a model id, or 'new'
  const [newFamily, setNewFamily] = useState('')
  const [justSaved, setJustSaved] = useState(null) // the save response, until the list includes it
  const dirty = useRef(false)
  useEffect(() => {
    const d = poll.data
    if (justSaved && (sel !== justSaved.id || (d && d.models.some(m => m.id === justSaved.id)))) setJustSaved(null)
  }, [poll.data, sel, justSaved])
  // leaving an edited form asks first; false means stay
  const leave = () => !dirty.current || window.confirm('Discard unsaved changes?')

  return (
    <div className="en imm">
      <Body state={poll}>
        {(d) => {
          const famIds = Object.keys(d.families)
          const current = sel && sel !== 'new'
            ? d.models.find(m => m.id === sel) || (justSaved && justSaved.id === sel ? justSaved : null) : null
          const startNew = newFamily && d.families[newFamily] ? newFamily : famIds[0]
          return (
            <>
              {!d.available.ok ? (
                <div className="imm-banner" role="status">
                  <Icon name="image" size={16} /><span>Pictures are unavailable: {d.available.reason}</span>
                </div>
              ) : null}
              <section className="en-sec">
                <h3 className="en-h">Models</h3>
                {d.models.length ? d.models.map(m => {
                  const prob = firstProblem(m)
                  return (
                    <button key={m.id} type="button" className={'imm-row' + (sel === m.id ? ' imm-row-sel' : '')}
                            onClick={() => { if (m.id !== sel && leave()) setSel(m.id) }}>
                      <span className="imm-star" aria-label={m.id === d.default ? 'default' : undefined}>
                        {m.id === d.default ? '★' : ''}</span>
                      <span className="imm-name">{m.name}</span>
                      <span className="imm-fam">{(d.families[m.family] || {}).label || m.family}</span>
                      {prob ? <span className="imm-state-bad" title={prob}>{prob}</span>
                            : <Chip tone="ok" dot>ready</Chip>}
                    </button>
                  )
                }) : <State kind="empty" title="No image models yet">Scan a folder below, or add one by hand.</State>}
                <div className="imm-new">
                  <Select aria-label="Family for a new model" value={startNew} onChange={(e) => {
                    const v = e.target.value
                    if (sel !== 'new') { setNewFamily(v); return } // only the next new model changes
                    if (dirty.current && !window.confirm('Switch family? Settings reset; the name and matching file paths are kept.')) return
                    setNewFamily(v)
                  }}>
                    {famIds.map(f => <option key={f} value={f}>{d.families[f].label}</option>)}
                  </Select>
                  <Button size="sm" className="imm-nowrap" onClick={() => { if (sel === 'new' || leave()) setSel('new') }}>New model</Button>
                </div>
              </section>
              {sel === 'new' || current ? (
                <section className="en-sec">
                  <h3 className="en-h">{current ? 'Edit model' : 'New model'}</h3>
                  <Editor key={current ? current.id : 'new'} familyId={current ? current.family : startNew} onDirty={(v) => { dirty.current = v }}
                          model={current || { name: '', family: startNew, files: {}, settings: {} }}
                          family={d.families[current ? current.family : startNew] || d.families[startNew]}
                          isDefault={!!current && current.id === d.default}
                          onChanged={(saved) => { if (saved && saved.id) { setJustSaved(saved); setSel(saved.id) } poll.refresh() }}
                          onGone={() => { setSel(null); poll.refresh() }} />
                </section>
              ) : null}
              <Scan families={d.families} models={d.models} onAdded={poll.refresh} />
            </>
          )
        }}
      </Body>
    </div>
  )
}
