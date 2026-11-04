/* selection — the small menu that follows a text selection in the Browser window. */
export const MAX_SELECTION = 200

/* the selection as one line, or '' when it is empty or too long to be a phrase */
export function cleanSelection(text) {
  const s = typeof text === 'string' ? text.replace(/\s+/g, ' ').trim() : ''
  return s && s.length <= MAX_SELECTION ? s : ''
}

/* Explore opens a Primer page on the phrase; Ask puts it in the ask box. */
export function selectionMenuItems(text) {
  if (!cleanSelection(text)) return []
  return [{ id: 'explore', label: 'Explore' }, { id: 'ask', label: 'Ask' }]
}

export const askPrefill = (text) => `About "${cleanSelection(text)}": `
