/* Graph — "Map of where I've been": every page visited, as a tree of how it was reached.
 * Clicking a row opens that page in a new Reader. Refreshes every 3 s while open. */
import { useMemo } from 'react'
import * as api from '../api.js'
import * as wm from '../windowManager.js'
import { Icon } from '../kit/icons.jsx'
import { Row, State } from '../kit/parts.jsx'
import { Body, usePoll } from './panel.jsx'
import { buildForest } from './graphTree.js'
import './apps.css'

/* how a page was reached: one existing kit glyph each; context wears the warm tone */
export const VIA = {
  start: { icon: 'launch', label: 'started here' },
  typed: { icon: 'console', label: 'typed' },
  context: { icon: 'story', label: 'from the surrounding context' },
  general: { icon: 'search', label: 'from a phrase' },
  sideways: { icon: 'forward', label: 'sideways' },
  kairos: { icon: 'chat', label: 'Kairos asked' },
}

const open = (id) => wm.open('reader', { props: { start: { node_id: id } } })

function Branch({ item, depth }) {
  const via = VIA[item.via] || VIA.general
  return (
    <>
      <div style={{ paddingLeft: depth * 20 }}>
        <Row onClick={() => open(item.node.id)}
             lead={<span className={'gp-via gp-via-' + item.via} title={via.label}><Icon name={via.icon} size={16} /></span>}
             title={<>
               {item.node.title}
               {item.alsoFrom.length ? <span className="gp-also">also reached from {item.alsoFrom.join(', ')}</span> : null}
             </>}
             meta={item.phrase || null} />
      </div>
      {item.children.map(c => <Branch key={c.node.id} item={c} depth={depth + 1} />)}
    </>
  )
}

function Forest({ snap }) {
  const forest = useMemo(() => buildForest(snap), [snap])
  if (!forest.length) return <State kind="empty" title="Nothing yet">Pages you visit will be mapped here.</State>
  return <div>{forest.map(r => <Branch key={r.node.id} item={r} depth={0} />)}</div>
}

export default function Graph() {
  const poll = usePoll(api.graph, 3000)
  return (
    <div className="gp">
      <Body state={poll}>{(snap) => <Forest snap={snap} />}</Body>
    </div>
  )
}
