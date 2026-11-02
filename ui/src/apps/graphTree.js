/* graphTree — turn the server's graph snapshot into the tree the map window draws.
 * Each page appears once, under the edge that first reached it; later edges from other
 * pages only add to its `alsoFrom`. Revisits repeat edges by design, so repeats collapse. */

export function buildForest(snapshot) {
  const nodes = new Map((snapshot?.nodes || []).map(n => [n.id, n]))
  const parentOf = new Map()                       // node id -> the first incoming edge
  const alsoFrom = new Map()                       // node id -> Set of other parents' ids
  for (const e of snapshot?.edges || []) {
    if (!nodes.has(e.to_id) || e.from_id === e.to_id) continue
    const rooted = e.from_id == null
    if (!rooted && !nodes.has(e.from_id)) continue
    if (!parentOf.has(e.to_id)) { parentOf.set(e.to_id, e); continue }
    if (rooted || e.from_id === parentOf.get(e.to_id).from_id) continue
    if (!alsoFrom.has(e.to_id)) alsoFrom.set(e.to_id, new Set())
    alsoFrom.get(e.to_id).add(e.from_id)
  }

  const kids = new Map()                           // parent id -> child ids, in edge order
  const roots = []
  for (const [id, e] of parentOf) {
    if (e.from_id == null) { roots.push(id); continue }
    if (!kids.has(e.from_id)) kids.set(e.from_id, [])
    kids.get(e.from_id).push(id)
  }

  // Only what hangs from a root is drawn, so a parent cycle with no root stays out of sight.
  const make = (id) => {
    const e = parentOf.get(id)
    return {
      node: nodes.get(id), via: e.via, phrase: e.phrase || '',
      alsoFrom: [...(alsoFrom.get(id) || [])].map(f => nodes.get(f).title),
      children: (kids.get(id) || []).map(make),
    }
  }
  return roots.map(make)
}
