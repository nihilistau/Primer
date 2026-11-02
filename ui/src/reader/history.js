/* history — one Reader window's back/forward list of pages. Entries are {nodeId, title}.
 * A push after going back drops the forward entries, as a browser does. */
export function createHistory() {
  let items = []
  let i = -1
  return {
    push(entry) { items = items.slice(0, i + 1); items.push(entry); i = items.length - 1 },
    replace(entry) { if (i < 0) this.push(entry); else items[i] = entry },
    back() { if (i > 0) i--; return i >= 0 ? items[i] : null },
    forward() { if (i < items.length - 1) i++; return i >= 0 ? items[i] : null },
    current: () => (i >= 0 ? items[i] : null),
    canBack: () => i > 0,
    canForward: () => i < items.length - 1,
  }
}
