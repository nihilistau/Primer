/* the Browser window's Live parts: the ask (escaped, Allow once and Stop post the right
 * answer), the live steps, and the Setup centre's line (server-side render: no DOM) */
import { afterEach, describe, expect, test, vi } from 'vitest'
import { renderToString } from 'react-dom/server'
import { AskPrompt, LiveSteps, answerLiveAsk } from './BrowserWindow.jsx'
import { LiveLine } from './SetupWindow.jsx'
import { liveFallback, pageTargets } from './live.js'

const flat = (el) => renderToString(el).replace(/<[^>]+>/g, ' ').replace(/&#x27;/g, "'").replace(/\s+/g, ' ').trim()

/* the buttons in a rendered element tree, by their text */
function buttons(el, out = {}) {
  if (!el || typeof el !== 'object') return out
  if (Array.isArray(el)) { el.forEach(c => buttons(c, out)); return out }
  if (typeof el.type === 'function') return buttons(el.type(el.props), out)
  const kids = el.props && el.props.children
  const text = [].concat(kids).filter(k => typeof k === 'string').join('')
  if (el.props && el.props.onClick && text) out[text] = el.props.onClick
  buttons(kids, out)
  return out
}

describe('the ask', () => {
  test('says what the model wants, with the label and value in bold', () => {
    const html = renderToString(<AskPrompt ask={{ id: 'a1', action: 'type', label: 'Search', value: 'Bodiam' }} onAnswer={() => {}} />)
    expect(html).toContain('<b>‘Bodiam’</b>')
    expect(html).toContain('<b>Search</b>')
    expect(flat(<AskPrompt ask={{ id: 'a1', action: 'press', label: 'Add to basket' }} onAnswer={() => {}} />))
      .toBe('The model wants to press Add to basket Allow once Stop')
  })
  test('a label or value that looks like HTML is shown as text, never as HTML', () => {
    const html = renderToString(<AskPrompt ask={{ id: 'a', action: 'type', label: '<img src=x onerror=alert(1)>', value: '<script>x</script>' }} onAnswer={() => {}} />)
    expect(html).not.toContain('<img')
    expect(html).not.toContain('<script')
    expect(html).toContain('&lt;img src=x onerror=alert(1)&gt;')
  })
  test('Allow once and Stop are real buttons', () => {
    const html = renderToString(<AskPrompt ask={{ id: 'a', action: 'press', label: 'Go' }} onAnswer={() => {}} />)
    expect(html.match(/<button type="button"[^>]*>Allow once<\/button>/)).toBeTruthy()
    expect(html.match(/<button type="button"[^>]*>Stop<\/button>/)).toBeTruthy()
  })

  afterEach(() => { vi.unstubAllGlobals() })
  test('Allow once posts allow: true, Stop posts allow: false, with the ask\'s id', async () => {
    const fetch = vi.fn(async () => ({ ok: true, json: async () => ({ ok: true }) }))
    vi.stubGlobal('fetch', fetch)
    const ask = { id: 'q-7', action: 'press', label: 'Book' }
    const tree = AskPrompt({ ask, onAnswer: (allow) => answerLiveAsk('live-abc123', ask, allow) })
    const b = buttons(tree)
    await b['Allow once']()
    await b['Stop']()
    expect(fetch).toHaveBeenCalledTimes(2)
    const [[u1, i1], [u2, i2]] = fetch.mock.calls
    expect(u1).toBe('/v1/live/live-abc123/agent/answer')
    expect(u2).toBe('/v1/live/live-abc123/agent/answer')
    expect(i1.method).toBe('POST')
    expect(i1.headers).toMatchObject({ 'X-Primer': '1', 'Content-Type': 'application/json' })
    expect(JSON.parse(i1.body)).toEqual({ id: 'q-7', allow: true })
    expect(JSON.parse(i2.body)).toEqual({ id: 'q-7', allow: false })
  })
})

describe('live steps', () => {
  test('what it saw, what it chose, why, and a refusal', () => {
    const t = flat(<LiveSteps steps={[
      { n: 1, saw: 'Bodiam Castle', url: 'https://a.test/', action: { action: 'follow', n: 2, label: 'Visit', href: 'https://a.test/v' }, why: 'hours are there' },
      { n: 2, saw: 'Visit', url: 'https://a.test/v', action: { action: 'type', n: 4, label: 'Password', text: 'x' }, why: 'w', refused: 'Primer never types into a password field.' },
    ]} />)
    expect(t).toContain('Bodiam Castle')
    expect(t).toContain('follow “Visit”')
    expect(t).toContain('hours are there')
    expect(t).toContain('Not done: Primer never types into a password field.')
    expect(renderToString(<LiveSteps steps={[]} />)).toBe('')
  })
})

describe('the Setup centre line', () => {
  test('the browser found, or what is needed', () => {
    expect(flat(<LiveLine live={{ available: true, label: 'Chrome 158', enabled: true }} />)).toBe('Live pages: Chrome 158')
    expect(flat(<LiveLine live={{ available: false, label: '', enabled: true }} />)).toBe('Live pages need Chrome or Edge')
    expect(renderToString(<LiveLine live={null} />)).toBe('')
  })
})

describe('Open it live for a failed reader page', () => {
  test('offered for a web address the site failed, or that gave no answer', () => {
    expect(liveFallback('https://shop.test/', { status: 502, message: 'HTTP 429 from shop.test' })).toBe('https://shop.test/')
    expect(liveFallback(' https://shop.test/a ', { status: 404, message: 'HTTP 404' })).toBe('https://shop.test/a')
    expect(liveFallback('http://shop.test/', new Error('Failed to fetch'))).toBe('http://shop.test/')
  })
  test('not for the reader’s own refusal of an address, a bad address or web search off', () => {
    expect(liveFallback('http://127.0.0.1:8810/', { status: 403, message: '127.0.0.1 is not a public address' })).toBe(null)
    expect(liveFallback('http://router/', { message: 'router is not a public address' })).toBe(null)
    expect(liveFallback('https://x.test/', { status: 403, message: 'the URL host is not plain' })).toBe(null)
    expect(liveFallback('https://x.test/', { status: 400, message: "can't open that kind of address" })).toBe(null)
    expect(liveFallback('https://x.test/', { status: 503, message: 'web search is off' })).toBe(null)
  })
  test('only web addresses', () => {
    expect(liveFallback('ref:Bodiam_Castle', { status: 502 })).toBe(null)
    expect(liveFallback('file:///c:/x', { status: 502 })).toBe(null)
    expect(liveFallback('', { status: 502 })).toBe(null)
  })
  test('a failed page can be opened live, but Ask and Make a page stay off until a page is open', () => {
    expect(pageTargets({ live: null, page: null, failed: 'https://shop.test/' })).toEqual({ cur: null, live: 'https://shop.test/' })
    expect(pageTargets({ live: null, page: null, failed: null })).toEqual({ cur: null, live: null })
    expect(pageTargets({ live: null, page: { u: 'https://a.test/' }, failed: 'https://shop.test/' })).toEqual({ cur: 'https://a.test/', live: 'https://a.test/' })
    expect(pageTargets({ live: { u: 'https://shop.test/' }, page: null, failed: null })).toEqual({ cur: 'https://shop.test/', live: 'https://shop.test/' })
    expect(pageTargets({ live: { u: 'about:blank' }, page: null, failed: null })).toEqual({ cur: null, live: null })
  })
})
