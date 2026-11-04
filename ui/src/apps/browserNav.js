/* browserNav — the Browser window's own history and address handling (pure). */

/* back/forward list of {u, title?}. A push after going back drops the forward entries; pushing
 * the address already shown updates it rather than adding a step. */
export function createNav() {
  let items = []
  let i = -1
  return {
    push(entry) {
      if (i >= 0 && items[i].u === entry.u) { items[i] = { ...items[i], ...entry }; return }
      items = items.slice(0, i + 1); items.push(entry); i = items.length - 1
    },
    replace(entry) { if (i < 0) this.push(entry); else items[i] = entry },
    peek: (d) => items[i + d] || null,
    move(d) { const k = i + d; if (k >= 0 && k < items.length) i = k },
    back() { if (i > 0) i--; return i >= 0 ? items[i] : null },
    forward() { if (i < items.length - 1) i++; return i >= 0 ? items[i] : null },
    current: () => (i >= 0 ? items[i] : null),
    canBack: () => i > 0,
    canForward: () => i < items.length - 1,
  }
}

export const isBrowseUrl = (u) => typeof u === 'string' && /^(https?:\/\/|kiwix:)/i.test(u)
export const kiwixHref = (u) => typeof u === 'string' && /^kiwix:/i.test(u)

/* What the address bar's text means: {u} to open, {search, where} to search, or {error}. */
export function addressFromInput(text, status) {
  const t = (text || '').trim()
  if (!t) return { error: 'Type an address or some words to look up.' }
  if (isBrowseUrl(t)) return { u: t }
  const ba = status && status.browse_available
  if (/^[a-z][a-z0-9+.-]*:(?!\d+(\/|$))/i.test(t)) return { error: "can't open that kind of address" }
  if (/^(localhost|[a-z0-9-]+(\.[a-z0-9-]+)*\.[a-z]{2,})(:\d+)?([/?#]\S*)?$/i.test(t)) return { u: 'https://' + t }
  if (!ba || !ba.ok) return { error: (ba && ba.reason) || 'The browser is not available.' }
  return { search: t, where: ba.web ? 'web' : 'reference' }
}

/* A source link on a page -> the address the Browser opens: a kiwix-serve content URL becomes
 * kiwix:<book>/<path>, a web URL stays itself, anything else is null. */
export function browseAddressFor(href, base) {
  if (typeof href !== 'string') return null
  const m = /^http:\/\/((?:127\.0\.0\.1|localhost|\[::1\])(?::\d+)?)\/content\/([^?#]+)/i.exec(href)
  // with the reference server's base URL known, only its own host:port is remapped
  let ours = true
  if (m && base) { try { ours = new URL(base).host.toLowerCase() === m[1].toLowerCase() } catch { ours = false } }
  if (m && ours) return 'kiwix:' + m[2]
  return /^https?:\/\//i.test(href) ? href : null
}

/* a click-through step's label: the server sends `link` as {n, text, url} (or null) */
export function stepLink(step) {
  const l = step && step.link
  if (l && typeof l === 'object') return String(l.text || l.url || '')
  return typeof l === 'string' ? l : ''
}

/* the x that keeps a menu of width w inside a container of width cw */
export const clampX = (x, w, cw) => Math.max(4, Math.min(x, cw - w - 4))


/* the page a click-through step puts in the window: the link it follows (the page the agent is
 * now reading), else the step's own url */
export function stepTarget(step) {
  if (!step) return ''
  if (step.action === 'follow' && step.link && typeof step.link === 'object' && step.link.url) return step.link.url
  return step.url || ''
}

/* a web page's title as a concept: a trailing " - Site", " | Site" or " — Site" (a short site
 * name of up to three words) is dropped; anything longer is part of the title */
export function pageConcept(title) {
  const t = (title || '').trim()
  const m = /^(.+?)\s+(?:[-|–—])\s+([^-|–—]{1,25})$/.exec(t)
  if (m && m[2].trim().split(/\s+/).length <= 3) return m[1].trim()
  return t
}
