import { expect, test } from 'vitest'
import { renderToStaticMarkup } from 'react-dom/server'
import { Markdown } from './markdown.jsx'

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
