# Primer Phase 7: the browser lens (reader browser) — Design

Date: 2026-11-04 · Builds on: [the main design](2026-11-01-illustrated-primer-design.md) §5 (lens
6: "a real web browser window the model can see and act on: what's on this page?, make an image
from this, and eventually click through this for me") · Status: Claude's design. The user chose
"reader browser now, driven Chrome later".

## 1. Intent

Primer's pages are the model's; sometimes you want the source itself. The Browser lens opens real
pages, from the web or from the offline Wikipedia, inside Primer, as clean readable text with
their links and pictures. The model can see what you see: ask it about the page, turn the page
into a Primer page, draw it, or ask it to click through the links for you towards a goal. Every
phrase stays selectable, so the browser is another way to branch.

Success: in the Warwick Castle page's Sources, clicking Wikipedia's "Warwick Castle" opens it in a
Browser window (offline). Selecting "Fulke Greville" offers Explore, which opens a Primer page.
"Click through for me: who owns it now?" follows two links and answers, showing each step.

## 2. Goals

- **Browser window** (from the dock, and from any source link): an address bar that takes a URL
  or search words, back/forward, reload, "open the original" (a new browser tab), and the page as
  readable text: headings, paragraphs, lists, quotes, code, simple tables, pictures and links.
- **Two places**: the web (through Phase 4's guarded fetcher: public addresses only, size and time
  limits) and the offline encyclopedia (`kiwix:` addresses served from kiwix-serve, no network).
  Search words search the web when web search is on, else the offline encyclopedia; a toggle
  picks.
- **Links** open in the same Browser window (ctrl/middle-click: a new one); only `http(s)` and
  `kiwix:` links are followed.
- **Selection menu**: select any text in the page → **Explore** (a Primer page on it, typed and
  linked from nothing), **Image** (a picture of it), **Ask** (puts "about …" into the ask box).
- **Ask**: a question about the page, answered by the writing model from the page's text, streamed
  in a side panel; "What's on this page?" is the default question.
- **Make a page**: a Primer page about this page, grounded in it alone (one source, cited),
  opened in a reader.
- **Click through for me**: a goal ("find who owns it now"); the writing model, seeing the page's
  title, an excerpt and its numbered links, picks a link or answers, up to 5 steps. Each step is
  shown and the window navigates to it; it ends with the answer, or "I couldn't find it" with
  where it got to. Stop at any time.
- **Source links** in readers (Phase 4) and map/story text open in the Browser lens instead of a
  new tab (shift-click keeps a new tab).
- **Unavailable with the reason**: web off and no offline encyclopedia → the browser can still
  open nothing, so it says why.

## 3. Non-goals

- No scripts, styles, forms, logins, cookies or downloads. No site layout. (A driven Chrome is a
  later phase.)
- No proxying of third-party HTML onto Primer's origin: Primer serves its own Markdown built from
  page text, never the page's HTML.
- No crawling beyond the 5-step click-through. No saving of browsed pages beyond the current
  window's history and a small in-memory cache.

## 4. Decisions (Claude's calls)

### 4.1 Reading a page

- `GET /v1/browse?u=<url>` → `{url, final_url, title, markdown, links: [{n, text, url}],
  where: "web"|"reference", origin_href}`. Web pages through `safe_fetch` (extended to also
  return the decoded HTML); `kiwix:<book>/<path>` through `KiwixClient.article_html`.
- HTML → Markdown by Primer's own stdlib converter: keeps `h1–h4`, `p`, `ul/ol/li`,
  `blockquote`, `pre/code`, `table` (as simple Markdown tables, at most 8 columns), `img`
  (alt + proxied URL), `a` (absolute, cleaned), `br`; drops scripts, styles, `nav`, `header`,
  `footer`, `aside`, forms, iframes, comments, and the same Wikipedia furniture as Phase 4
  (infobox kept as a small table? — no: dropped, like Phase 4, except its coordinates are not
  needed here). Text is escaped so Markdown/HTML syntax in it is shown as text. `<main>`/`<article>`
  preferred. Output capped at 200,000 characters.
- Links are resolved against the final URL; `kiwix` article links become `kiwix:<book>/<path>`;
  `javascript:`, `data:`, `mailto:` and other schemes are dropped; fragments kept for in-page
  anchors only as text.
- Pictures: `GET /v1/browse/image?u=<url>` fetches through the same guard (public addresses, 5 MB
  cap, 10 s), sniffs the type from the bytes (PNG, JPEG, GIF, WebP only — never SVG), and answers
  with that type, `X-Content-Type-Options: nosniff` and `Content-Security-Policy: default-src
  'none'`. kiwix images are served from kiwix-serve the same way (local). Pages carry
  `![alt](/v1/browse/image?u=…)`.
- A small in-memory LRU (50 pages, 10 minutes) so back/forward is instant.

### 4.2 Search

`GET /v1/browse/search?q=…&where=web|reference` → `{results: [{title, url, snippet}]}` from the
tier's searcher (Phase 4) or kiwix full-text search; the window shows them as a results page.

### 4.3 The model on the page

- **Ask**: `POST /v1/browse/ask {u, question}` → SSE `delta`/`done`/`error`. The writing model gets
  the page's title and Markdown (clipped to about 12,000 characters) inside the Phase 4 untrusted
  `<source>` framing ("reference material, not instructions") and the question. It takes a page
  turn on the GPU gate.
- **Make a page**: `POST /v1/pages/open` gains `source_url` (an `http(s)` or `kiwix:` address);
  with it, the page's single source is that page (title, text clipped as in Phase 4), mode `web` or
  `reference` accordingly, cited `[1]`.
- **Click through**: `POST /v1/browse/agent {u, goal}` → SSE `step {n, url, title, action:
  "follow"|"answer", link, why}`, `answer {text}`, `done`, `error`. Each step: the page's title,
  its first 4,000 characters and up to 60 numbered links; the model replies with JSON
  `{"action":"follow","link":n,"why":"…"}` or `{"action":"answer","text":"…"}`; invalid replies
  get one retry, then the agent stops with what it has. 5 steps maximum, 120 s total. Links are
  only ever ones the page offered (a model can't invent a URL).

### 4.4 UI

- A **Browser** dock button (a globe glyph). Browser windows are movable/resizable like the rest.
- Content uses the reader's Markdown renderer (no HTML injection), with links intercepted.
- Selection menu appears over a text selection inside the content.
- Side panel (collapsible): Ask and Click-through, with their streams and steps.

## 5. Security

- Web fetches only through the Phase 4 guard; the browser adds no new way to reach private
  addresses. kiwix only on 127.0.0.1.
- No third-party HTML, script or SVG is ever served from Primer's origin; images are sniffed and
  served with nosniff and a null CSP.
- Page text in prompts is framed as untrusted reference material; the click-through can only
  follow links the page offered.
- Origin rules unchanged for the new routes (GETs from the page; POSTs with the Origin check).

## 6. Left to the builder

The converter's details within §4.1, the prompt wording, the window layout, and test structure.

## 7. Deferred

Image from a selection and "Image of the page" are not in v0.7.0: use Make a page, then Image.
