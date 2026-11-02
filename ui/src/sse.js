/* sse — parse a Server-Sent-Events byte stream arriving in arbitrary chunks.
 * parseSSE(chunkText, carry) -> { events: [{event, data}], carry }
 * `carry` is the incomplete tail of the previous call; pass it back with the next chunk.
 * Each event is `event: <kind>\ndata: <json>\n\n`; data is JSON-parsed (raw text if it isn't JSON). */
export function parseSSE(chunk, carry = '') {
  const text = (carry + chunk).replace(/\r\n/g, '\n')
  const parts = text.split('\n\n')
  const rest = parts.pop()
  const events = []
  for (const block of parts) {
    let event = 'message'
    const data = []
    for (const line of block.split('\n')) {
      if (line.startsWith(':')) continue
      if (line.startsWith('event:')) event = line.slice(6).trim()
      else if (line.startsWith('data:')) data.push(line.slice(5).replace(/^ /, ''))
    }
    if (!data.length) continue
    const raw = data.join('\n')
    let parsed = raw
    try { parsed = JSON.parse(raw) } catch { /* keep the text */ }
    events.push({ event, data: parsed })
  }
  return { events, carry: rest }
}
