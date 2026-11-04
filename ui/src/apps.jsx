/* apps — the registry. A window is an instance of one of these. `single` apps (Graph,
 * Engines) keep one window; Reader opens a new instance every time. */
import Reader from './reader/Reader.jsx'
import Graph from './apps/Graph.jsx'
import Engines from './apps/Engines.jsx'
import ImageModels from './apps/ImageModels.jsx'
import ImageWindow from './apps/ImageWindow.jsx'
import VideoWindow from './apps/VideoWindow.jsx'
import MapWindow from './apps/MapWindow.jsx'
import StoryWindow from './apps/StoryWindow.jsx'
import BrowserWindow from './apps/BrowserWindow.jsx'
import SetupWindow from './apps/SetupWindow.jsx'

export const APPS = {
  reader: { title: 'New page', icon: 'story', size: [720, 640], component: Reader },
  graph: { title: 'Map of where I’ve been', icon: 'apps', size: [720, 560], component: Graph, single: true },
  engines: { title: 'Engines', icon: 'models', size: [560, 520], component: Engines, single: true },
  imagemodels: { title: 'Models', icon: 'tools', size: [600, 640], component: ImageModels, single: true },
  // multi-instance: one window per image job, props { jobId, subject? }
  image: { title: 'Image', icon: 'image', size: [640, 720], component: ImageWindow },
  // multi-instance: one window per video job, props { jobId, subject? }
  video: { title: 'Video', icon: 'stage', size: [640, 760], component: VideoWindow },
  // multi-instance: one window per map job, props { mapId, subject?, nodeId? }
  map: { title: 'Map', icon: 'map', size: [760, 640], component: MapWindow },
  // multi-instance: one window per story (or Listen), props { storyId, subject?, nodeId?, kind? }
  // multi-instance: one window per browser, props { start? } (an address to open first)
  browser: { title: 'Browser', icon: 'globe', size: [900, 680], component: BrowserWindow },
  story: { title: 'Story', icon: 'speaker', size: [720, 740], component: StoryWindow },
  // kits, downloads, approvals, moving models, updates (Phase 9); opens itself on first run
  setup: { title: 'Setup', icon: 'download', size: [700, 740], component: SetupWindow, single: true },
}

/* the dock's buttons, in order */
export const DOCK = ['reader', 'browser', 'graph', 'engines', 'imagemodels', 'setup']

/* open an app through the window manager with its registry defaults */
export function openApp(wm, appId, props) {
  const a = APPS[appId]
  return wm.open(appId, { props, single: !!a.single, w: a.size[0], h: a.size[1] })
}
