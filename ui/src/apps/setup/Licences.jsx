/* one checkbox per distinct licence, each with its text; accepting is recorded at once and
 * can't be taken back here (Setup asks once per licence) */
import { safeHref } from '../../reader/citations.js'
import { licenceKey } from '../setupPlan.js'

export default function Licences({ licences, disabled, onAccept }) {
  const left = licences.filter(l => !l.accepted).length
  return (
    <>
      <ul className="su-lics">
        {licences.map(l => {
          const href = safeHref(l.link)
          return (
            <li key={licenceKey(l)}>
              <label className="su-skip">
                <input type="checkbox" checked={!!l.accepted} disabled={disabled || l.accepted}
                       onChange={() => onAccept(licenceKey(l))} />
                I accept <b>{l.licence}</b>
              </label>
              {href ? <a className="su-link" href={href} target="_blank" rel="noopener noreferrer" title={href}>read it</a> : null}
            </li>
          )
        })}
      </ul>
      <div className="en-note">{left ? `${left} still to accept before downloading.` : 'All accepted.'}</div>
    </>
  )
}
