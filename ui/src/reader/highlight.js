/* highlight — find the model's chosen phrases in a run of text.
 * Case-insensitive, whole words only, longest phrase first, never overlapping. Only the
 * first occurrence of a phrase on the page is marked: `seen` (a Set of lower-cased phrases)
 * is shared by every text node of the page and updated here. Returns the text cut into
 * plain strings and {phrase, text} marks. */
const esc = (s) => s.replace(/[\\^$.*+?()[\]{}|]/g, '\\$&')

export function splitByPhrases(text, phrases, seen) {
  const found = []
  const byLength = [...new Set(phrases.map(p => p.trim()).filter(Boolean))].sort((a, b) => b.length - a.length)
  for (const phrase of byLength) {
    const key = phrase.toLowerCase()
    if (seen.has(key)) continue
    const re = new RegExp(String.raw`(?<![\p{L}\p{N}_])${esc(phrase)}(?![\p{L}\p{N}_])`, 'giu')
    for (let m; (m = re.exec(text));) {
      const end = m.index + m[0].length
      if (found.some(f => m.index < f.end && end > f.start)) continue
      found.push({ start: m.index, end, phrase })
      seen.add(key)
      break
    }
  }
  found.sort((a, b) => a.start - b.start)
  const out = []
  let at = 0
  for (const f of found) {
    if (f.start > at) out.push(text.slice(at, f.start))
    out.push({ phrase: f.phrase, text: text.slice(f.start, f.end) })
    at = f.end
  }
  if (at < text.length) out.push(text.slice(at))
  return out.length ? out : [text]
}
