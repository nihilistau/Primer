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
