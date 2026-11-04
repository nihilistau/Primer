import { expect, test } from 'vitest'
import { renderToStaticMarkup } from 'react-dom/server'
import { Markdown, followLink } from './markdown.jsx'

test('raw HTML in markdown renders as text', () => {
  const html = renderToStaticMarkup(<Markdown source={'<img src=x onerror=alert(1)>'} phrases={[]} />)
  expect(html).not.toContain('<img')
})
test('renders structure and marks a phrase once', () => {
  const src = '# Title\n\nA **bold** castle and a castle.\n\n- one\n- two\n\n> quote\n\n---\n\n`code`'
  const html = renderToStaticMarkup(<Markdown source={src} phrases={['castle']} onPhrase={() => {}} />)
  expect(html).toContain('<h1')
  expect(html).toContain('<strong>bold</strong>')
  expect(html).toContain('<li>')
  expect(html).toContain('<blockquote')
  expect(html).toContain('<hr')
  expect(html).toContain('<code>code</code>')
  expect(html.match(/rd-phrase/g)).toHaveLength(1)
})
test('citations and highlighted phrases both render', () => {
  const sources = [{ n: 1, title: 'A', href: 'http://k/a' }, { n: 2, title: 'B' }]
  const html = renderToStaticMarkup(
    <Markdown source={'The castle fell [1] and the moat dried [2], see [5].'} phrases={['castle']} sources={sources} onPhrase={() => {}} />)
  expect(html).toContain('rd-phrase')
  expect(html).toMatch(/<sup[^>]*><a href="http:\/\/k\/a" target="_blank" rel="noopener noreferrer"[^>]*>1<\/a><\/sup>/)
  expect(html).toMatch(/<sup[^>]*>2<\/sup>/)
  expect(html).toContain('[5]')
})
test('a non-http href produces no link', () => {
  const sources = [{ n: 1, title: 'A', href: 'javascript:alert(1)' }, { n: 2, title: 'B', href: 'data:text/html,x' }]
  const html = renderToStaticMarkup(<Markdown source={'a [1] b [2]'} sources={sources} />)
  expect(html).not.toContain('<a')
  expect(html).not.toContain('javascript:')
  expect(html).toMatch(/<sup[^>]*>1<\/sup>/)
})

test('onLink: http(s) and kiwix links are links; other schemes and images are inert', () => {
  const src = '[ok](https://a.test/x) [wiki](kiwix:b/A/C) [bad](javascript:alert(1)) [rel](/v1/x) ![pic](/v1/browse/image?u=abc) ![evil](https://t.test/p.png)'
  const html = renderToStaticMarkup(<Markdown source={src} onLink={() => {}} />)
  expect(html).toContain('<a href="https://a.test/x"')
  expect(html).toContain('<a href="kiwix:b/A/C"')
  expect(html).not.toContain('javascript:')
  expect(html).not.toContain('href="/v1/x"')
  expect(html).toContain('bad')
  expect(html).toMatch(/<img [^>]*src="\/v1\/browse\/image\?u=abc"/)
  expect(html).not.toContain('t.test/p.png')
  expect(html).toContain('evil')
})
test('without onLink, links and images stay as plain text', () => {
  const html = renderToStaticMarkup(<Markdown source={'[ok](https://a.test/x) ![pic](/v1/browse/image?u=abc)'} />)
  expect(html).not.toContain('<a')
  expect(html).not.toContain('<img')
})
test('onLink: a click calls onLink with the url, ctrl or middle asks for a new window', () => {
  const calls = []
  const onLink = (u, o) => calls.push([u, o])
  const ev = (o = {}) => ({ ctrlKey: false, metaKey: false, button: 0, preventDefault() { this.prevented = true }, ...o })
  const a = ev(), b = ev({ ctrlKey: true }), c = ev({ button: 1 })
  followLink(a, 'https://a.test/x', onLink); followLink(b, 'kiwix:b/A/C', onLink); followLink(c, 'https://a.test/y', onLink)
  expect(calls).toEqual([['https://a.test/x', { newWindow: false }], ['kiwix:b/A/C', { newWindow: true }], ['https://a.test/y', { newWindow: true }]])
  expect(a.prevented && b.prevented && c.prevented).toBe(true)
})


test('a table renders as a table with its cell text, links following the onLink rules', () => {
  const src = ['| Name | Where |', '|:--|--:|', '| Castle | [Site](https://a.test/x) |', '| Keep | here |'].join(String.fromCharCode(10))
  const html = renderToStaticMarkup(<Markdown source={src} onLink={() => {}} />)
  expect(html).toContain('<table')
  expect(html).toContain('<th')
  expect(html).toContain('Castle')
  expect(html).toContain('href="https://a.test/x"')
  expect(html).not.toContain('|')
  const plain = renderToStaticMarkup(<Markdown source={src} />)
  expect(plain).not.toContain('<a ')
})
