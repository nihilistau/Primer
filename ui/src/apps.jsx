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
import TimelineWindow from './apps/TimelineWindow.jsx'
import DiagramWindow from './apps/DiagramWindow.jsx'
import BrowserWindow from './apps/BrowserWindow.jsx'
import SetupWindow from './apps/SetupWindow.jsx'
import ReaderWindow from './apps/ReaderWindow.jsx'
import ReviewWindow from './apps/ReviewWindow.jsx'
import QuestWindow from './apps/QuestWindow.jsx'
import TaleWindow from './apps/TaleWindow.jsx'

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
  // multi-instance: one window per timeline (or All my timelines), props { timelineId?, subject?, nodeId?, all? }
  timeline: { title: 'Timeline', icon: 'timeline', size: [820, 560], component: TimelineWindow },
  // multi-instance: one window per diagram, props { diagramId, subject?, nodeId? }
  diagram: { title: 'Diagram', icon: 'diagram', size: [760, 680], component: DiagramWindow },
  // multi-instance: one window per story (or Listen, or a Tale's chapter), props { storyId, subject?, nodeId?, kind?, chapter? }
  // multi-instance: one window per browser, props { start? } (an address to open first)
  browser: { title: 'Browser', icon: 'globe', size: [900, 680], component: BrowserWindow },
  story: { title: 'Story', icon: 'speaker', size: [720, 740], component: StoryWindow },
  // kits, downloads, approvals, moving models, updates (Phase 9); opens itself on first run
  setup: { title: 'Setup', icon: 'download', size: [700, 740], component: SetupWindow, single: true },
  // the reader model, spaced review of Test me questions, and the Quest (Phase 10)
  readermodel: { title: 'Reader', icon: 'anon', size: [560, 680], component: ReaderWindow, single: true },
  review: { title: 'Review', icon: 'memory', size: [600, 680], component: ReviewWindow, single: true },
  // props { fromNodeId? }: build from that page's trail
  quest: { title: 'Quest', icon: 'games', size: [600, 720], component: QuestWindow, single: true },
  // the continuing story: its chapters, the story bible, the next chapter (Phase 11)
  tale: { title: 'Tale', icon: 'journal', size: [640, 760], component: TaleWindow, single: true },
}

/* the dock's buttons, in order */
export const DOCK = ['reader', 'browser', 'graph', 'readermodel', 'review', 'quest', 'tale', 'engines', 'imagemodels', 'setup']

/* open an app through the window manager with its registry defaults */
export function openApp(wm, appId, props) {
  const a = APPS[appId]
  return wm.open(appId, { props, single: !!a.single, w: a.size[0], h: a.size[1] })
}
