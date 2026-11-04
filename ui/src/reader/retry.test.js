import { expect, test } from 'vitest'
import { retryBody } from './retry.js'

test('a node-addressed request is resent as it was', () => {
  expect(retryBody({ node_id: 'n1', regenerate: true }, 'n1')).toEqual({ node_id: 'n1', regenerate: true })
  expect(retryBody({ node_id: 'n1', depth: 'deeper' }, 'n1')).toEqual({ node_id: 'n1', depth: 'deeper' })
})
test('a fresh open becomes {node_id} once the node is known', () => {
  const req = { concept: 'Castle', via: 'typed' }
  expect(retryBody(req, 'n2')).toEqual({ node_id: 'n2' })
  expect(retryBody(req, null)).toBe(req)
})

test('a make-a-page request keeps its source_url and regenerate on retry', () => {
  const req = { concept: 'Castle', via: 'typed', source_url: 'https://e.org/c', regenerate: true }
  expect(retryBody(req, 'n3')).toEqual(req)
})
