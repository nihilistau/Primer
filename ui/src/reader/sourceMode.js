/* sourceMode — labels for the reader's source badge and the "Rewrite from" menu. */
const plural = (n) => `${n} source${n === 1 ? '' : 's'}`

export function badgeLabel(mode, count) {
  if (mode === 'reference') return `offline reference · ${plural(count || 0)}`
  if (mode === 'web') return `web · ${plural(count || 0)}`
  return mode || ''
}

const OPTIONS = [['storyteller', 'Storyteller'], ['reference', 'Offline reference'], ['web', 'Web']]

/* One option per mode; a mode the status says is unavailable is disabled with its reason. */
export function rewriteOptions(status) {
  const src = status && status.sources
  return OPTIONS.map(([mode, label]) => {
    const s = src && src[mode]
    if (!s) {
      // no status yet: only the storyteller is known to work
      const ok = mode === 'storyteller'
      return { mode, label, disabled: !ok, reason: ok ? '' : 'Source status unknown' }
    }
    return { mode, label, disabled: !s.available, reason: s.available ? '' : (s.reason || 'Unavailable') }
  })
}
