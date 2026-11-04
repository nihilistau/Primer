import { expect, test } from 'vitest'
import { createNav, addressFromInput, browseAddressFor, isBrowseUrl, kiwixHref, stepLink, clampX } from './browserNav.js'

test('history: push, back, forward, and a push drops the forward entries', () => {
  const h = createNav()
  expect(h.current()).toBe(null)
  h.push({ u: 'https://a.test/' }); h.push({ u: 'https://b.test/' }); h.push({ u: 'https://c.test/' })
  expect(h.back().u).toBe('https://b.test/')
  expect(h.canForward()).toBe(true)
  h.push({ u: 'https://d.test/' })
  expect(h.canForward()).toBe(false)
  expect(h.back().u).toBe('https://b.test/')
  expect(h.forward().u).toBe('https://d.test/')
})
test('history: pushing the address already shown replaces it, not a duplicate step', () => {
  const h = createNav()
  h.push({ u: 'https://a.test/' }); h.push({ u: 'https://a.test/', title: 'A' })
  expect(h.canBack()).toBe(false)
  expect(h.current().title).toBe('A')
})
test('history: replace swaps the current entry', () => {
  const h = createNav()
  h.push({ u: 'x' }); h.replace({ u: 'x', title: 'X' })
  expect(h.current()).toEqual({ u: 'x', title: 'X' })
})

const web = { browse_available: { ok: true, web: true, reference: true } }
const offline = { browse_available: { ok: true, web: false, reference: true } }
const none = { browse_available: { ok: false, reason: 'nope', web: false, reference: false } }

test('addressFromInput: URLs and kiwix addresses are opened as typed', () => {
  expect(addressFromInput(' https://example.com/a b ', web)).toEqual({ u: 'https://example.com/a b' })
  expect(addressFromInput('kiwix:wiki/Warwick', web)).toEqual({ u: 'kiwix:wiki/Warwick' })
})
test('addressFromInput: a bare domain gets https', () => {
  expect(addressFromInput('example.com', web)).toEqual({ u: 'https://example.com' })
  expect(addressFromInput('docs.example.org/x?y=1', web)).toEqual({ u: 'https://docs.example.org/x?y=1' })
  expect(addressFromInput('localhost:8080', web)).toEqual({ u: 'https://localhost:8080' })
})
test('addressFromInput: words search the web, or the offline encyclopedia without it', () => {
  expect(addressFromInput('warwick castle', web)).toEqual({ search: 'warwick castle', where: 'web' })
  expect(addressFromInput('warwick castle', offline)).toEqual({ search: 'warwick castle', where: 'reference' })
  expect(addressFromInput('e.g. why', web)).toEqual({ search: 'e.g. why', where: 'web' })
})
test('addressFromInput: empty, unsupported schemes and an unavailable browser are errors', () => {
  expect(addressFromInput('   ', web).error).toBeTruthy()
  expect(addressFromInput('javascript:alert(1)', web).error).toMatch(/kind of address/)
  expect(addressFromInput('file:///etc/passwd', web).error).toMatch(/kind of address/)
  expect(addressFromInput('cats', none).error).toBe('nope')
})
test('browseAddressFor maps source hrefs to browser addresses', () => {
  expect(browseAddressFor('http://127.0.0.1:8830/content/wikipedia_en/A/Warwick_Castle')).toBe('kiwix:wikipedia_en/A/Warwick_Castle')
  expect(browseAddressFor('http://localhost:8830/content/b/A/R%C3%A9sum%C3%A9#x')).toBe('kiwix:b/A/R%C3%A9sum%C3%A9')
  expect(browseAddressFor('https://example.com/x')).toBe('https://example.com/x')
  expect(browseAddressFor('http://example.com/content/a/b')).toBe('http://example.com/content/a/b')
  expect(browseAddressFor('javascript:alert(1)')).toBe(null)
  expect(browseAddressFor(undefined)).toBe(null)
})
test('isBrowseUrl and kiwixHref', () => {
  expect(isBrowseUrl('https://a.test')).toBe(true)
  expect(isBrowseUrl('kiwix:b/A/C')).toBe(true)
  expect(isBrowseUrl('javascript:x')).toBe(false)
  expect(isBrowseUrl('//evil.test')).toBe(false)
  expect(kiwixHref('kiwix:b/A/C')).toBe(true)
  expect(kiwixHref('https://a.test')).toBe(false)
})
test('stepLink: the server sends link as an object', () => {
  expect(stepLink({ link: { n: 3, text: 'Michelangelo', url: 'kiwix:b/A/M' } })).toBe('Michelangelo')
  expect(stepLink({ link: { n: 3, url: 'kiwix:b/A/M' } })).toBe('kiwix:b/A/M')
  expect(stepLink({ link: null })).toBe('')
  expect(stepLink({ link: 'plain' })).toBe('plain')
  expect(stepLink(undefined)).toBe('')
})
test('browseAddressFor only remaps the reference server when its base is known', () => {
  const h = 'http://127.0.0.1:8830/content/b/A/C'
  expect(browseAddressFor(h, 'http://127.0.0.1:8830')).toBe('kiwix:b/A/C')
  expect(browseAddressFor(h, 'http://127.0.0.1:9999')).toBe(h)
})
test('clampX keeps a menu inside its container', () => {
  expect(clampX(500, 100, 400)).toBe(296)
  expect(clampX(-5, 100, 400)).toBe(4)
  expect(clampX(50, 100, 400)).toBe(50)
})
test('peek and move: the index only changes when move is called', () => {
  const h = createNav()
  h.push({ u: 'a' }); h.push({ u: 'b' })
  expect(h.peek(-1).u).toBe('a')
  expect(h.current().u).toBe('b')
  h.move(-1)
  expect(h.current().u).toBe('a')
  expect(h.peek(-1)).toBe(null)
  h.move(-1)
  expect(h.current().u).toBe('a')
})


import { stepTarget, pageConcept } from './browserNav.js'
test('stepTarget: a follow step loads the link it follows, else the page url', () => {
  expect(stepTarget({ action: 'follow', url: 'https://a/1', link: { n: 2, url: 'https://a/2' } })).toBe('https://a/2')
  expect(stepTarget({ action: 'answer', url: 'https://a/1' })).toBe('https://a/1')
  expect(stepTarget({ action: 'follow', url: 'https://a/1', link: null })).toBe('https://a/1')
})
test('pageConcept drops a trailing site name', () => {
  expect(pageConcept('Whale - Wikipedia')).toBe('Whale')
  expect(pageConcept('Castles | Example Site')).toBe('Castles')
  expect(pageConcept('Castles — BBC')).toBe('Castles')
  expect(pageConcept('Tom - Jerry and the long story of a cat')).toBe('Tom - Jerry and the long story of a cat')
  expect(pageConcept('Wikipedia')).toBe('Wikipedia')
  expect(pageConcept('')).toBe('')
})
