/* the plan's items by role: name, size, licence, state, and per item Skip and Swap (either
 * re-plans). Items already on disk say so; skipped items are listed after, to bring back. */
import { Chip, Select } from '../../kit/parts.jsx'
import { safeHref } from '../../reader/citations.js'
import { formatBytes, groupByRole, itemState } from '../setupPlan.js'

const ROLE_NAMES = {
  text: 'Writing', fast: 'Fast jobs', voice: 'Voice', reference: 'Reference', map: 'Map', runtime: 'Programs',
  image: 'Pictures', video: 'Video', fonts: 'Map fonts',
}
// image, image_encoder and image_vae are one group; runtime_llama and runtime_sd another
const family = (i) => String(i.role || 'other').split('_')[0]
const roleName = (r) => ROLE_NAMES[r] || r

function LicenceLink({ licence, link }) {
  const href = safeHref(link)
  if (!licence) return null
  return href
    ? <a className="su-link" href={href} target="_blank" rel="noopener noreferrer" title={href}>{licence}</a>
    : <span>{licence}</span>
}

function Item({ item, run, disabled, onChoose }) {
  const s = itemState(run ? { ...item, ...run } : item)
  const choices = item.choices || []
  return (
    <div className="su-item" data-item={item.id}>
      <div className="su-item-top">
        <span className="su-item-name">{item.label}</span>
        <Chip tone={s.tone} dot busy={s.busy}>{s.label}</Chip>
        {item.gated ? <Chip tone="warn">gated</Chip> : null}
        <span className="su-item-size">{formatBytes(item.size)}</span>
      </div>
      <div className="su-item-meta">
        <LicenceLink licence={item.licence} link={item.licence_link} />
        {item.md5 && !item.sha256 ? <span>checked by MD5</span> : null}
        {item.use_path ? <span>uses the llama-server on the PATH</span> : null}
        {choices.length > 1 ? (
          <Select aria-label={`${item.label}: variant`} value={item.choice} disabled={disabled}
                  onChange={(e) => onChoose(item.id, e.target.value)}>
            {choices.map(c => <option key={c.id} value={c.id}>{c.label}{c.size ? ` (${formatBytes(c.size)})` : ''}</option>)}
          </Select>
        ) : null}
        <label className="su-skip">
          <input type="checkbox" checked={false} disabled={disabled} onChange={() => onChoose(item.id, 'skip')} />
          Skip
        </label>
      </div>
      {item.note ? <div className="en-note">{item.note}</div> : null}
      {item.error ? <div className="en-err">{item.error}</div> : null}
    </div>
  )
}

export default function PlanItems({ plan, run, labels, disabled, onChoose }) {
  const byId = new Map((run || []).map(r => [r.id, r]))
  return (
    <>
      {groupByRole(plan.items, family).map(g => (
        <div key={g.role} className="su-role">
          <h4 className="su-role-h">{roleName(g.role)}</h4>
          {g.items.map(i => <Item key={i.id} item={i} run={byId.get(i.id)} disabled={disabled} onChoose={onChoose} />)}
        </div>
      ))}
      {(plan.skipped || []).length ? (
        <div className="su-role">
          <h4 className="su-role-h">Skipped</h4>
          {plan.skipped.map(id => (
            <div key={id} className="su-item su-item-skipped">
              <div className="su-item-top">
                <span className="su-item-name">{(labels || {})[id] || id}</span>
                <label className="su-skip">
                  <input type="checkbox" checked disabled={disabled}
                         onChange={() => onChoose(id, null)} />
                  Skip
                </label>
              </div>
            </div>
          ))}
        </div>
      ) : null}
    </>
  )
}
