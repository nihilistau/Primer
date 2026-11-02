import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import { _reset, getWindows } from './windowManager.js'
import { MEDIA_EVENT, OPEN_PLACE_EVENT, openImageWindow, openMapWindow, openPlace, openVideoWindow } from './openImage.js'

let target, heard
beforeEach(() => {
  _reset()
  target = new EventTarget(); heard = 0
  target.addEventListener(MEDIA_EVENT, () => { heard++ })
  vi.stubGlobal('window', target)
})
afterEach(() => vi.unstubAllGlobals())

test('opening an Image or Video window announces new media', () => {
  openImageWindow('m1', 'a mill')
  expect(heard).toBe(1)
  openVideoWindow('m2', 'a mill')
  expect(heard).toBe(2)
  expect(getWindows().map(w => [w.appId, w.props.jobId])).toEqual([['image', 'm1'], ['video', 'm2']])
})

test('opening a Map window announces new media and carries the map and its node', () => {
  openMapWindow('mp1', 'Warwickshire', 'n1')
  expect(heard).toBe(1)
  const w = getWindows()[0]
  expect([w.appId, w.props]).toEqual(['map', { mapId: 'mp1', subject: 'Warwickshire', nodeId: 'n1' }])
})

test('a place opens in the reader that shows the mapped page, else in a new reader', () => {
  const body = { concept: 'Warwick Castle', via: 'general', from_id: 'n1', phrase: 'Warwick' }
  let got
  target.addEventListener(OPEN_PLACE_EVENT, (e) => { got = e.detail; e.detail.handled = true })
  openPlace(body)
  expect(got.body).toEqual(body)
  expect(getWindows()).toHaveLength(0)
  openPlace(body, { newWindow: true })           // shift/middle click: always a new reader
  expect(getWindows().map(w => [w.appId, w.props.start])).toEqual([['reader', body]])
})

test('a place with no reader to take it opens a new reader', () => {
  const body = { concept: 'Kenilworth', via: 'general', from_id: 'n1', phrase: 'Kenilworth' }
  openPlace(body)
  expect(getWindows().map(w => [w.appId, w.props.start])).toEqual([['reader', body]])
})
