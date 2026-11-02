/* apps — the registry. A window is an instance of one of these. `single` apps (Graph,
 * Engines) keep one window; Reader opens a new instance every time. */
import Reader from './reader/Reader.jsx'
import Graph from './apps/Graph.jsx'
import Engines from './apps/Engines.jsx'
import ImageModels from './apps/ImageModels.jsx'
import ImageWindow from './apps/ImageWindow.jsx'

export const APPS = {
  reader: { title: 'New page', icon: 'story', size: [720, 640], component: Reader },
  graph: { title: 'Map of where I’ve been', icon: 'apps', size: [720, 560], component: Graph, single: true },
  engines: { title: 'Engines', icon: 'models', size: [560, 520], component: Engines, single: true },
  imagemodels: { title: 'Image models', icon: 'tools', size: [600, 640], component: ImageModels, single: true },
  // multi-instance: one window per image job, props { jobId, subject? }
  image: { title: 'Image', icon: 'image', size: [640, 720], component: ImageWindow },
}

/* the dock's buttons, in order */
export const DOCK = ['reader', 'graph', 'engines', 'imagemodels']

/* open an app through the window manager with its registry defaults */
export function openApp(wm, appId, props) {
  const a = APPS[appId]
  return wm.open(appId, { props, single: !!a.single, w: a.size[0], h: a.size[1] })
}
