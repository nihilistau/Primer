/* executables waiting for approval. Setup's own come grouped by release, approved together;
 * files Setup didn't install are shown apart, with a warning, and approved one at a time. */
import { Button, Chip } from '../../kit/parts.jsx'
import { groupApprovals, shortSha } from '../setupPlan.js'

const copy = (sha) => { try { navigator.clipboard.writeText(sha).catch(() => {}) } catch { /* no clipboard here */ } }

/* the short form to spot, the full SHA-256 below it to compare with the publisher's */
function File({ r, action }) {
  return (
    <div className="su-filebox">
      <div className="su-file">
        <span className="su-file-name" title={r.path}>{r.file}</span>
        <span className="su-sha-short">{shortSha(r.sha256)}…</span>
        {r.changed ? <Chip tone="warn">changed since installed</Chip> : null}
        <Button size="sm" variant="ghost" onClick={() => copy(r.sha256)} aria-label={`Copy the SHA-256 of ${r.file}`}>Copy</Button>
        {action}
      </div>
      <div className="su-sha" aria-label={`SHA-256 of ${r.file}`}>SHA-256 {r.sha256}</div>
    </div>
  )
}

export default function Approvals({ pending, busy, onApproveRelease, onApprove }) {
  const { releases, unknown } = groupApprovals(pending)
  return (
    <>
      <div className="en-note">Nothing Setup downloaded runs until it is approved here, by its SHA-256.</div>
      {releases.map(g => (
        <div key={g.key} className="su-rel">
          <div className="su-item-top">
            <span className="su-item-name">{g.publisher}</span>
            <Chip>{g.release}</Chip>
            <span className="su-item-size">{g.files.length} file{g.files.length === 1 ? '' : 's'}</span>
            <Button size="sm" disabled={busy} onClick={() => onApproveRelease(g.item)}>Approve all from this release</Button>
          </div>
          {g.files.map(r => <File key={r.path} r={r} />)}
        </div>
      ))}
      {unknown.length ? (
        <div className="su-rel su-rel-warn">
          <div className="su-item-top">
            <Chip tone="warn" dot>not installed by Setup</Chip>
          </div>
          <div className="en-reason">
            These files sit beside Setup&rsquo;s programs, but Setup didn&rsquo;t put them there. Approve one only if you know where it came from.
          </div>
          {unknown.map(r => (
            <File key={r.path} r={r}
                  action={<Button size="sm" disabled={busy} onClick={() => onApprove(r)}>Approve</Button>} />
          ))}
        </div>
      ) : null}
    </>
  )
}
