import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import { _reset, getWindows } from './windowManager.js'
import { MEDIA_EVENT, openImageWindow, openVideoWindow } from './openImage.js'

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
