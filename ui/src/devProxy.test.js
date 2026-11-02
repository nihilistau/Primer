import { expect, test } from 'vitest'
import { proxyOrigin } from '../vite.config.js'

const T = 'http://127.0.0.1:8810'

test('the dev page\'s own Origin is rewritten to the target (Primer then accepts it)', () => {
  expect(proxyOrigin('http://localhost:5173', 'localhost:5173', T)).toBe(T)
  expect(proxyOrigin('http://127.0.0.1:5173', '127.0.0.1:5173', T)).toBe(T)
})
test('any other Origin passes through untouched (Primer refuses it with 403)', () => {
  expect(proxyOrigin('http://localhost:3000', 'localhost:5173', T)).toBe('http://localhost:3000')
  expect(proxyOrigin('https://evil.example', 'localhost:5173', T)).toBe('https://evil.example')
  expect(proxyOrigin('null', 'localhost:5173', T)).toBe('null')
})
test('no Origin stays absent', () => {
  expect(proxyOrigin(undefined, 'localhost:5173', T)).toBe(undefined)
})
