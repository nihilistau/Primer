/* streamEnd — how a page stream ended. The server always sends `done` or `error` before it
 * closes, so a stream that ends with neither was cut off (Primer stopped, the network
 * dropped) and the page must not look finished. Returns the error to show, or ''. */
export const CUT_OFF = 'The connection closed before the page finished.'

export function streamEndError({ done, error }) {
  return done || error ? '' : CUT_OFF
}
