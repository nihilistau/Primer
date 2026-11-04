/* Setup — choose a kit and a folder, check the plan (sizes, licences, what is already here),
 * download it, approve the executables it brought, and switch to the tier it wrote. Two more
 * tabs move model files to another drive and check for newer builds. The server checks
 * everything again; this window says what it will say, sooner. Polls once a second only
 * while a run is going or paused. */
import { useEffect, useRef, useState } from 'react'
import * as api from '../api.js'
import { Button, Chip, Input, Tabs } from '../kit/parts.jsx'
import { Body, usePoll } from './panel.jsx'
import {
  downloadBlockedReason, errText, formatBytes, licencesToAccept, planHeld, planTotals, rememberLabels, withChoice,
} from './setupPlan.js'
import { markSetupOpened } from './setupSession.js'
import KitPicker from './setup/KitPicker.jsx'
import PlanItems from './setup/PlanItems.jsx'
import Licences from './setup/Licences.jsx'
import Progress from './setup/Progress.jsx'
import Approvals from './setup/Approvals.jsx'
import MoveModels from './setup/MoveModels.jsx'
import Updates from './setup/Updates.jsx'
import './apps.css'

const TABS = [{ id: 'install', label: 'Install' }, { id: 'move', label: 'Move models' }, { id: 'updates', label: 'Updates' }]
const ACTIVE = ['running', 'paused']

const ErrLine = ({ err, at }) => (err.at === at && err.text ? <div className="en-err" role="alert">{err.text}</div> : null)

export function Install({ data, refresh }) {
  const st = data.state || {}
  const [kit, setKit] = useState(st.kit || (data.suggested && data.suggested.kit) || '')
  const [root, setRoot] = useState(st.root || data.root_default || '')
  const [choices, setChoices] = useState(st.choices || {})
  const [planResp, setPlanResp] = useState(null)   // POST /v1/setup/plan's answer (with `fits`)
  // the folder the plan on show was checked for (a restored plan: the installer's root)
  const [planRoot, setPlanRoot] = useState(st.plan ? (st.root || '') : null)
  const [labels, setLabels] = useState({})          // item id -> label, for items later skipped
  const seq = useRef(0)                             // only the newest plan request may land
  const [planError, setPlanError] = useState('')
  const [planning, setPlanning] = useState(false)
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState({ at: '', text: '' })   // shown under the section that acted
  const [switched, setSwitched] = useState('')

  const running = st.state === 'running' || st.state === 'done'
  // a paused run holds its plan (a failed one doesn't: nothing runs, and the server re-plans)
  const held = planHeld(st)
  const locked = running || held
  // the plan on show: the one just checked, else the one the installer kept (a restored run)
  const plan = planResp || st.plan || null
  const totals = planTotals(plan, planResp && planResp.fits ? planResp.fits.free_bytes : data.free_bytes)
  const toAccept = licencesToAccept(plan, st.licences || [])
  const stale = !!plan && planRoot !== null && root.trim() !== planRoot.trim()
  const blocked = downloadBlockedReason({ plan, totals, toAccept, running, planError, root, planRoot: planRoot ?? undefined })
  useEffect(() => { setLabels(k => rememberLabels(k, plan)) }, [plan])

  const replan = async (k, c, r) => {
    if (!k) return
    const mine = ++seq.current
    setPlanning(true); setPlanError('')
    try {
      const p = await api.setupPlan(k, c, r.trim())
      if (mine !== seq.current) return            // a newer request is on its way
      setPlanResp(p); setChoices(c); setPlanRoot(r.trim())
    } catch (e) {
      if (mine !== seq.current) return
      setPlanError(errText(e))
    } finally {
      if (mine === seq.current) { setPlanning(false); refresh() }
    }
  }
  const act = async (at, fn) => {
    setBusy(true); setErr({ at: '', text: '' })
    try { await fn() } catch (e) { setErr({ at, text: errText(e) }) }
    setBusy(false)
    refresh()
  }

  const pickKit = (id) => { setKit(id); replan(id, id === kit ? choices : {}, root) }
  const choose = (id, value) => replan(kit, withChoice(choices, id, value), root)
  // a paused or failed run resumes from Progress; Download starts one
  const startable = !['running', 'paused', 'done', 'failed', 'ready'].includes(st.state)
  const gated = plan && (plan.items || []).some(i => i.gated)

  return (
    <>
      <section className="en-sec">
        <h3 className="en-h">Kit</h3>
        <KitPicker kits={data.kits || []} suggested={data.suggested} hardware={data.hardware}
                   value={kit} disabled={locked || planning} onPick={pickKit} />
      </section>

      <section className="en-sec">
        <h3 className="en-h">Destination</h3>
        <div className="su-dest">
          <Input aria-label="Destination folder" value={root} spellCheck={false} disabled={locked}
                 placeholder="a folder on a fixed drive" onChange={(e) => setRoot(e.target.value)}
                 onKeyDown={(e) => { if (e.key === 'Enter') replan(kit, choices, root) }} />
          <Button disabled={locked || planning || !kit || !root.trim()} onClick={() => replan(kit, choices, root)}>
            {planning ? 'Checking…' : plan ? 'Check again' : 'Check the plan'}
          </Button>
        </div>
        <div className="en-note">
          {stale ? 'check the plan to see the free space in this folder'
            : totals.free === null ? 'free space unknown' : `${formatBytes(totals.free)} free`}
          {plan && plan.checked ? ` · catalogues checked ${plan.checked}` : ''}
        </div>
        {planError ? <div className="en-err" role="alert">{planError}</div> : null}
        {held ? <div className="en-reason">This plan is held by the {st.state} run: resume it (or cancel its items) before changing the kit, the folder or the items.</div> : null}
      </section>

      {plan ? (
        <section className="en-sec">
          <h3 className="en-h">{plan.label || plan.kit}: what it holds</h3>
          <PlanItems plan={plan} run={st.state !== 'idle' ? st.items : null} labels={labels}
                     disabled={locked || planning} onChoose={choose} />
          <div className={'su-totals' + (stale ? ' su-stale' : '')} title={stale ? 'for the folder checked before; check the plan again' : undefined}>
            <span>To download <b>{formatBytes(totals.toDownload)}</b></span>
            <span>needs <b>{formatBytes(totals.needed)}</b> with room to unpack</span>
            <span>free <b>{totals.free === null ? 'unknown' : formatBytes(totals.free)}</b></span>
            {totals.fits === false ? <Chip tone="err" dot>short by {formatBytes(totals.shortfall)}</Chip> : null}
            {totals.fits === true ? <Chip tone="ok" dot>fits</Chip> : null}
          </div>
          {gated ? (
            <div className="en-note">
              Some items are gated on Hugging Face.{' '}
              {data.token ? 'A Hugging Face token is present.' : 'No Hugging Face token found: put one in var/keys/huggingface.key.'}
            </div>
          ) : null}
        </section>
      ) : null}

      {plan && (st.licences || []).length ? (
        <section className="en-sec">
          <h3 className="en-h">Licences</h3>
          <Licences licences={st.licences} disabled={busy}
                    onAccept={(l) => act('licences', () => api.setupLicence(l))} />
          <ErrLine err={err} at="licences" />
        </section>
      ) : null}

      {plan && startable ? (
        <section className="en-sec">
          <div className="su-go">
            <Button variant="primary" icon="download" disabled={busy || !!blocked} title={blocked || undefined}
                    onClick={() => act('go', () => api.setupStart(kit, (planRoot ?? root).trim(), choices))}>Download</Button>
            {blocked ? <span className="su-why">{blocked}</span> : null}
          </div>
          <ErrLine err={err} at="go" />
        </section>
      ) : null}

      {st.state && st.state !== 'idle' ? (
        <section className="en-sec">
          <h3 className="en-h">Progress</h3>
          <Progress st={st} busy={busy} onPause={() => act('progress', api.setupPause)}
                    onResume={() => act('progress', api.setupResume)}
                    onCancel={(id) => act('progress', () => api.setupCancel(id))} />
          <ErrLine err={err} at="progress" />
        </section>
      ) : null}

      {(st.approvals || []).length ? (
        <section className="en-sec">
          <h3 className="en-h">Approve the programs</h3>
          <Approvals pending={st.approvals} busy={busy}
                     onApproveRelease={(item) => act('approvals', () => api.setupApproveRelease(item))}
                     onApprove={(r) => act('approvals', () => api.setupApprove(r.path, r.sha256))} />
          <ErrLine err={err} at="approvals" />
        </section>
      ) : null}

      {st.state === 'ready' && st.tier ? (
        <section className="en-sec">
          <h3 className="en-h">Ready</h3>
          <div className="su-go">
            <Button disabled={busy || switched === st.tier}
                    onClick={() => act('ready', async () => { await api.setTier(st.tier); setSwitched(st.tier) })}>
              Switch to the new tier
            </Button>
            <Chip tone="accent">{st.tier}</Chip>
            {switched === st.tier ? <Chip tone="ok" dot>switched</Chip> : null}
          </div>
          <div className="en-note">
            The Models window has the picture and video models Setup found
            {(st.models_added || []).length ? `: ${st.models_added.join(', ')}` : ''}.
          </div>
          <ErrLine err={err} at="ready" />
        </section>
      ) : null}
    </>
  )
}

export default function SetupWindow() {
  const [tab, setTab] = useState('install')
  const [seen, setSeen] = useState(() => new Set(['install']))   // a tab mounts on its first visit
  const show = (id) => { setTab(id); setSeen(s => (s.has(id) ? s : new Set([...s, id]))) }
  const [ms, setMs] = useState(0)
  const poll = usePoll(api.setup, ms)
  const state = poll.data && poll.data.state && poll.data.state.state
  // once a second while something is happening, otherwise only when the window acts
  useEffect(() => { setMs(ACTIVE.includes(state) || state === 'done' ? 1000 : 0) }, [state])
  const marked = useRef(false)
  useEffect(() => { if (!marked.current) { marked.current = true; markSetupOpened() } }, [])

  return (
    <div className="en su">
      <Tabs tabs={TABS} value={tab} onChange={show} label="Setup" />
      {/* a tab stays mounted once seen, so a checked plan or a chosen move survives a look elsewhere */}
      <div hidden={tab !== 'install'}>
        <Body state={poll}>{(data) => <Install data={data} refresh={poll.refresh} />}</Body>
      </div>
      <div hidden={tab !== 'move'}>{seen.has('move') ? <MoveModels /> : null}</div>
      <div hidden={tab !== 'updates'}>{seen.has('updates') ? <Updates /> : null}</div>
    </div>
  )
}
