/* geometry — where the branch fork's cards and curves go. Pure: no DOM.
 * layoutFork(anchor, container, n, card) -> { cards: [{x, y}], paths: [d], actions: {x, y, w, flipped}, column }
 * The cards sit in a row below the anchor (the clicked word's box), or above it when
 * there is no room below. Three cards that cannot fit side by side stack in a column.
 * Everything is clamped inside the container; a curve runs from the anchor's
 * bottom-centre (top-centre when flipped) to each card's near edge, centre. */
export const GAP = 12        // between cards
export const LIFT = 36       // anchor edge to the nearest card edge: room for the curves
export const ACTIONS_H = 32  // the Image / Video / Ask row
const MARGIN = 8             // card to container edge

const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v))

export function layoutFork(anchor, container, n, card = { w: 200, h: 64 }) {
  const { w, h } = card
  const rowW = n * w + (n - 1) * GAP
  const column = n > 1 && rowW > container.w - 2 * MARGIN
  const blockH = (column ? n * h + (n - 1) * GAP : h) + 8 + ACTIONS_H

  const below = anchor.y + anchor.h + LIFT
  const roomBelow = container.h - below
  const roomAbove = anchor.y - LIFT
  // below if it fits, else above if it fits, else whichever side has more room
  const flipped = roomBelow < blockH && (roomAbove >= blockH || roomAbove > roomBelow)

  const cx = anchor.x + anchor.w / 2
  const sx = cx
  const sy = flipped ? anchor.y : anchor.y + anchor.h
  // the block's top edge (cards first when below, the action row first when flipped),
  // kept inside the container; the curves absorb any shortfall. A block taller than the
  // container is pinned to the top.
  const top = clamp(flipped ? anchor.y - LIFT - blockH : below, 0, Math.max(0, container.h - blockH))
  const cardsTop = flipped ? top + ACTIONS_H + 8 : top

  // Narrower than w + 2*MARGIN, cards are pinned at MARGIN and overflow the right edge.
  const maxX = (width) => Math.max(MARGIN, container.w - width - MARGIN)
  let x0, xs, side = null
  if (column) {
    // the column sits beside the anchor, so the three curves fan out to its near edge
    const xRight = maxX(w), xLeft = MARGIN
    side = cx <= xRight ? 'right' : cx >= xLeft + w ? 'left' : 'right'   // 'right' also when cramped
    x0 = side === 'right' ? xRight : xLeft
    xs = () => x0
  } else {
    x0 = clamp(cx - rowW / 2, MARGIN, maxX(rowW))
    xs = i => x0 + i * (w + GAP)
  }
  const cards = Array.from({ length: n }, (_, i) =>
    ({ x: xs(i), y: column ? cardsTop + i * (h + GAP) : cardsTop }))

  const paths = cards.map(c => {
    if (column) {
      // leave the anchor vertically, arrive horizontally at the facing edge, mid-height
      const ex = side === 'right' ? c.x : c.x + w
      const ey = c.y + h / 2
      return `M${sx} ${sy} C${sx} ${ey}, ${sx + (ex - sx) * 0.4} ${ey}, ${ex} ${ey}`
    }
    const tx = c.x + w / 2
    const ty = flipped ? c.y + h : c.y
    const my = sy + (ty - sy) * 0.55
    return `M${sx} ${sy} C${sx} ${my}, ${tx} ${my}, ${tx} ${ty}`
  })

  const last = column ? cards[n - 1].y + h : cardsTop + h
  const actions = { x: x0, y: flipped ? top : last + 8, w: column ? w : rowW, flipped }
  return { cards, paths, actions, column }
}
