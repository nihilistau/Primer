/* the kits as cards, the one suggested for this machine first, with the reason */
import { Chip } from '../../kit/parts.jsx'
import { formatBytes } from '../setupPlan.js'

export default function KitPicker({ kits, suggested, hardware, value, disabled, onPick }) {
  const sid = suggested && suggested.kit
  const ordered = [...kits].sort((a, b) => (b.id === sid) - (a.id === sid))
  return (
    <>
      <div className="su-kits" role="group" aria-label="Kits">
        {ordered.map(k => (
          <button key={k.id} type="button" className={'su-kit' + (k.id === value ? ' su-kit-on' : '')}
                  aria-pressed={k.id === value} disabled={disabled} onClick={() => onPick(k.id)}>
            <span className="su-kit-top">
              <span className="su-kit-name">{k.label}</span>
              {k.id === sid ? <Chip tone="accent">suggested</Chip> : null}
              <span className="su-kit-size">about {formatBytes(k.size_hint)}</span>
            </span>
            <span className="su-kit-sum">{k.summary}</span>
          </button>
        ))}
      </div>
      <div className="en-note">
        {suggested && suggested.reason ? suggested.reason
          : hardware && hardware.gpu ? `${hardware.gpu}, ${hardware.vram_gb} GB` : 'no GPU found'}
      </div>
    </>
  )
}
