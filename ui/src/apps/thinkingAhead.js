/* thinkingAhead — pure labels for the Engines "Thinking ahead" row. */

export const THINKING_OPTIONS = [
  { value: 0, label: 'Off' },
  { value: 1, label: 'One page' },
  { value: 2, label: 'Two pages' },
]

/** The setting to show for a status payload (0 when the server sent none). */
export function thinkingSetting(st) {
  const v = st && st.thinking_ahead && st.thinking_ahead.setting
  return v === 1 || v === 2 ? v : 0
}

/** "N written · M used · hit rate X%", or '' before anything was written. */
export function thinkingStats(ta) {
  if (!ta || !ta.written) return ''
  const rate = typeof ta.hit_rate === 'number' ? ` · hit rate ${Math.round(ta.hit_rate * 100)}%` : ''
  return `${ta.written} written · ${ta.used || 0} used${rate}`
}

/** "drafting: <title>" while a page is being written ahead, else ''. */
export function thinkingNow(ta) {
  return ta && ta.drafting ? `drafting: ${ta.drafting}` : ''
}

/** The GPU row's extra words: the idle GPU is busy writing ahead. */
export function gpuBackgroundNote(gpu) {
  return gpu && gpu.background ? 'thinking ahead' : ''
}
