# Primer Phase 12: more lenses — timeline and diagrams — Design

Date: 2026-11-09 · Builds on: [the roadmap](2026-11-04-primer-roadmap.md) Phase 12 and the map
lens (Phase 5) · Status: Claude's design; the user asked to start Phase 12.

## 1. Intent

Primer can show a page as text, pictures, video, a map and a story. Two kinds of knowledge still
fit none of those well: **when** things happened, and **how** something works. Phase 12 adds two
lenses: a **Timeline** of the events a page names, each opening its own page, and a **Diagram**
of a mechanism or process, drawn by the writing model as clean labelled lines rather than a
painted picture (image models get mechanisms wrong), with every label clickable.

Success: on "Rhuddlan Castle", **Timeline** shows the castle's dated events in order in a few
seconds (1277 begun, 1282 the Welsh revolt, 1284 the Statute of Rhuddlan, 1648 slighted), each a
pin that opens a page; **All my timelines** lays every timeline so far on one axis. On "how does
a trebuchet work", **Diagram** draws the arm, pivot, counterweight, sling and projectile with
arrows for the motion, every part labelled and clickable, in well under a minute.

## 2. Goals

- **Timeline lens** (a reader toolbar button, like Map; and typed: "timeline of the Tudors"):
  - the fast model lists the dated events the page names (and, for a grounded page, its sources'
    text): a short label, the date as written, and a normalised date: year (negative for BCE),
    with month and day when given, and a precision (day, month, year, decade, century);
    "the 12th century" → 1150 with century precision, "c. 1277" → 1277 approximate;
  - a **Timeline window**: a horizontal axis that fits the span (zoom and pan), events as pins
    with labels that don't overlap (stacked rows), approximate dates drawn as faint spans; click
    an event → a page about it ("in this context" of the page it came from);
  - **All my timelines**: the events of every finished timeline on one axis, coloured by page;
  - typed requests: "timeline of X", "when did X happen", "X timeline" → the timeline of X's page
    (opening the page first if needed), like the map's typed requests.
- **Diagram lens** (a reader toolbar button; and typed: "diagram of how a trebuchet works",
  "draw a diagram of X", "how does X work" stays a page):
  - the writing model (a page request at the GPU gate) draws an **SVG** of the thing the page
    explains: parts labelled with `<text>`, arrows for motion or flow, at most ~40 shapes, a
    1000×700 view box, black lines on a light ground; it may choose a parts diagram or a flow
    (process) diagram;
  - the SVG is **parsed and rebuilt from an allowlist** (no scripts, styles, links, images,
    foreignObject, event handlers, external references or `url()`); anything else is dropped;
    a drawing with fewer than 3 labels, or unparseable after one retry, fails with the reason;
  - a **Diagram window** shows it scaled to fit, with **every label clickable** (opens a page
    about that part, in this context), zoom, **Again** (a fresh drawing), and the caption the
    model gives in one sentence;
  - a phrase can be diagrammed too (from the branch cards, like Image/Video): "the sling".
- Both are stored per page version like maps, show in the page's media tray, and survive restart.

## 3. Safety

- The diagram SVG is untrusted model output: rebuilt from parsed elements and attributes on an
  allowlist (svg, g, line, polyline, polygon, rect, circle, ellipse, path, text, tspan, title,
  defs, marker, use of a local marker only); numeric attributes must be numbers; `d` and
  `points` only path/number characters; `transform` only translate/rotate/scale with numbers;
  colours only `#hex`, named basic colours or `none`; `marker-end`/`marker-start` only
  `url(#id)` pointing at a marker defined in the same drawing; text content is plain text. The
  window renders the rebuilt SVG; nothing from the model reaches the DOM unparsed.
- Timeline labels are plain text; event pages open through the normal page route.
- Page text and sources are quoted to both models as material, never instructions.

## 4. Decisions (Claude's calls)

- **Fast model for timelines** (extraction is short JSON, like the map's place list); **writing
  model for diagrams** (drawing needs the stronger model).
- **SVG over Mermaid**: Mermaid would need a renderer library in the page and only draws boxes
  and arrows; SVG lets the model draw a mechanism, and an allowlist rebuild keeps it safe.
- **Our own date normaliser** after the model's guess: the model gives the date as written and
  its reading; we re-parse common forms ("1277", "c. 1277", "1277–1283", "12th century",
  "300 BC", "May 1282") and keep the model's reading only when ours can't parse.
- **No grounding lookups per event** in this phase: the page's own text (and its sources when
  grounded) is the ground; events are only those the text names.

## 5. Non-goals

- No editing of diagrams; no diagram from a picture.
- No timeline of pages read by date read (that's the trail, already in the Map of where I've
  been).

## 6. Left to the builder

Prompt wording, the timeline layout algorithm, the exact allowlist details beyond §3, window
layouts, and test structure.
