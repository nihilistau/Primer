/* citations — split page text into plain strings and {n} citation markers. Only a bare
 * [n] with 1 <= n <= count counts; [1, 2], [^1], [x] and invented numbers stay text. */
export function splitCitations(text, count) {
  const s = text || ''
  if (!s) return []
  if (!(count >= 1)) return [s]
  const out = []
  let last = 0
  for (const m of s.matchAll(/\[(\d{1,3})\]/g)) {
    const n = Number(m[1])
    if (n < 1 || n > count) continue
    if (m.index > last) out.push(s.slice(last, m.index))
    out.push({ n })
    last = m.index + m[0].length
  }
  if (last < s.length) out.push(s.slice(last))
  return out
}

/* Defence in depth: only an http(s) URL is ever put in an href. */
export const safeHref = (h) => (typeof h === 'string' && /^https?:\/\//i.test(h) ? h : null)
