# Changelog

## v0.13.0: your book (2026-11-10)

Your trail, bound as a book.

### Added

- **Book** (a button in the dock): make a book from what you've read. Choose everything, a page
  and what branched from it, or everything since a date; set the title and the By line; switch
  pictures, maps, timelines, diagrams and stories on or off. A preview line says how many
  chapters and pictures, and about how big. The book is made in the background ("EPUB: chapter 12
  of 40") and comes as an **EPUB 3** for e-readers and phones and a single **HTML** file for any
  browser.
- Each page you read is a chapter, in trail order, with a "From phrase in Parent page" line, the
  page text, citations linked to a numbered Sources list, its pictures, each finished map drawn
  as a plate from the offline world map with numbered pins and a list of places, its timeline as
  a list of dates, and its diagram.
- Stories and Tale chapters go in a Stories part, text only. The cover has the title, the By
  line, the date range and the first picture. The contents follow the trail, at most 6 deep. A
  closing note says what the limits left out.
- Earlier books are listed in the window, can be downloaded again and can be deleted after a
  confirm step. Books are kept in `var/books/`.
- Routes: `GET /v1/books`, `GET /v1/books/pages`, `POST /v1/books/preview`, `POST /v1/books`,
  `GET /v1/books/<id>` with `/epub` and `/html`, and `POST /v1/books/<id>/delete`.
- A book needs no model and no GPU. The reader notes and profile, quiz items, quests and the
  Tale's bible are never put in a book.

### Measured

On the local-gpu tier, with the real trail: everything was 42 chapters and 12 pictures, an EPUB
of 26 MB and an HTML file of 62 MB, made in about 2 seconds. A branch from Trebuchet was 9
chapters in about 1 second. Since 8 November without pictures was an EPUB of 19 KB and an HTML
file of 33 KB. Every map plate was drawn from the world map in well under a second.

### Known

- The tests check the EPUB's structure, but epubcheck is not run on it.
- A page with several finished maps gets a plate for each one.

## v0.12.0: timelines and diagrams (2026-11-09)

Two more ways to look at a page.

### Added

- **Timeline** (in a page's header): the dates a page names on an axis, points for years and
  bars for spans, centuries and reigns, before the common era too. Zoom, pan, Fit; click an event
  to read about it. **All my timelines** puts every page's latest timeline on one axis, coloured
  by page. Type "timeline of the Tudors" to open a page and its timeline together.
- **Diagram** (in a page's header and on branch forks): a labelled line drawing of how a thing
  works, with a caption. Click a label to read about that part. Type "diagram of a trebuchet" or
  "show me a diagram of how a siphon works".
- Every diagram is rebuilt from an allowlist of plain shapes, text and arrowheads on the server
  and checked again in the window, so a drawing can't run code, load anything or link anywhere.
- `/v1/intent` always answers with `timeline` and `diagram` alongside the other lenses.

### Fixed during the live run

- A small fast model that thinks aloud no longer loses a timeline: citation marks like `[1]`,
  a quoted "reply []", an array of one-event arrays and a run of arrays with no outer brackets
  are all read correctly, and an answer that can't be read is asked once more of the writing
  model.

### Measured

On the local-gpu tier: a timeline in 10 to 30 seconds on LFM3 1.7B (about 100 when it thinks
aloud), a diagram in 12 to 20 seconds on gemma-4-E4B.

## v0.11.0: the continuing story (2026-11-09)

One story that goes on as you read.

### Added

- **The Tale** (a journal in the dock): start a story with a hero of your own, or let Primer
  invent one, and each **Next chapter** carries it on from the pages you've read since the last
  chapter, keeping their facts true: 3 to 5 scenes, illustrated and read aloud in the Story
  window.
- **A story bible** you can see and edit: the hero's name, whether they're female or male, description and look,
  companions, places, things carried, open threads and a summary of each chapter, updated after
  every chapter.
- **The same hero in every picture**: each chapter picture starts with the hero's look (and a
  companion's when they're in the scene), drawn as a storybook scene.
- Chapters appear in the stories of every page they drew on; older tales are kept and can be
  reopened.

### Measured

With gemma-4-E4B: a tale started in about 5 seconds, a chapter's scenes in 10 to 12 seconds and
its bible update in about 6 more.

## v0.10.1: Primer and Kairos (2026-11-08)

The first link to Kairos, the companion: she can know what you read, and look at Primer.

### Added

- **Telling Kairos what you read** (`primer/activity.py`), off unless `PRIMER_ACTIVITY_DIR` is set:
  each reading signal (a page opened, Deeper, Simpler, Regenerate, a quiz answer) is also appended
  to Kairos's activity record as `primer/<day>.jsonl`, with the page title and its node id.
  `PRIMER_ACTIVITY_PRIVATE=1` marks every row private, which Kairos never reads. A failed write
  never costs a reading signal.
- **Primer as an MCP server** (`python -m primer.mcp_server`): one tool, `primer(action, args)`,
  with a schema that won't change as actions are added; `help`, `status`, `page`, `reader`,
  `quiz_due` and `quest`, all read-only, answered from a running Primer's localhost API.
  Stdlib only. Setup is in `docs/RUNNING.md`, under Kairos.

## v0.10.0: the Primer knows you, and asks back (2026-11-05)

Pages shaped to how you read, and questions about what you've read.

### Added

- **Reader** (a person in the dock): what Primer has noticed about how you read (Deeper or
  Simpler, which branches you follow, what you read most and longest, what you know and found
  hard) as a short summary in plain words and the counts behind it; your own notes, which always
  come first; a switch to use it in pages or not; and Reset. Page prompts, branch cards and
  highlights are shaped by it, as preferences that can't override a page's rules or citations.
- **Test me** on any page: a recall, an explain-why and a connect-to-an-earlier-page question,
  your typed answers marked Right, Partly or Not yet against the page with a line quoting it.
- **Review** (a brain in the dock, with the number due): questions come back after 1, 3, 7, 21
  and 60 days as you get them right, sooner when you don't.
- **Quest** (a pawn in the dock, or from a page): up to five rooms built from your trail, each
  door opened by a question about its page, with hints, a nudge to reread, and Skip after three
  tries.
- Everything stays on your machine; Reset forgets it all and keeps your pages.

### Measured

With gemma-4-E4B writing and LFM3 1.7B for the fast jobs: three questions in about 4 seconds,
an answer marked in about 1 second, a five-room quest built in about 15 seconds, a reader
summary in about 10 seconds on the CPU.

## v0.9.0: the setup centre (2026-11-05)

From a fresh copy to a working Primer in one window.

### Added

- **Setup** (an arrow in the dock, and opened by itself the first time no writing model is
  available): pick a starter kit suggested from your graphics card (Light about 5 GB, Hybrid
  about 27 GB, Workstation about 63 GB), choose a folder, skip or swap any item, accept the
  licences and press Download. Models come from Hugging Face, the programs (llama.cpp,
  stable-diffusion.cpp, kiwix-tools) from their publishers, Wikipedia from Kiwix and the map from
  Protomaps, looked up live so the kits don't go stale.
- **Downloads you can leave**: one at a time in the background with speed and time left; pause,
  resume and restarts carry on where they stopped (the map cut starts again if a newer world
  build appeared meanwhile). Files already in the folder aren't fetched again. A plan that
  couldn't write a tier (no writing model, or no llama.cpp) is refused before anything downloads.
- **Checked before use**: every file must match its publisher's SHA-256 (MD5 for kiwix-tools)
  before it is moved into place; zips are unpacked safely; pickle formats are refused.
- **Programs need your approval**: Primer never runs a program or DLL it downloaded until you
  approve it by its SHA-256, per release or one at a time, and refuses one that has changed since.
- **A ready tier**: when a kit is in, Setup writes `tiers/local-<kit>.toml` with every path, port
  and setting filled in (thinking off for LFM3, CPU servers kept off the GPU), adds the kit's
  picture and video models to Models, and offers to switch to it.
- **Move models**: copy model files off slow drives (external USB disks are spotted even though
  Windows calls them fixed), checking each copy and pointing Models and Setup's tiers at it. The
  originals are kept and your own tiers are never edited.
- **Check for updates**: newer Wikipedia files and program releases, never installed on their own.

## v0.8.0: thinking ahead (2026-11-04)

The next page is often already written.

### Added

- **Thinking ahead**: while you read, the idle GPU writes the page you're most likely to open next
  (the first phrase's In this context card; with Two pages, its General card too). Opening it is
  instant. Opening one still being written shows what's there and streams the rest.
- **Never in the way**: drafts run only when nothing else wants the GPU and the writing model
  isn't paused for pictures, and stop within about a second (with a local model server) when you
  ask for a page, picture or clip. Pages written ahead stay invisible until you open them.
- **Engines → Thinking ahead**: Off, One page or Two pages, with pages written, used and the hit
  rate. Branch cards written ahead show a small mark. On by default only when the writing model
  runs on this machine's GPU in auto mode; hosted and CPU-only tiers start with it off.

### Fixed

- Pictures made for a topic with no page stay in the map of where you've been.

### Measured

With gemma-4-E4B: a drafted card opened in 0.00 s instead of about 8 s; a page asked for during a
draft waited 0.25 s for it to step aside.

## v0.7.0: the browser (2026-11-04)

Real pages, read inside Primer.

### Added

- **Browser** (a globe in the dock): web pages and offline Wikipedia articles (`kiwix:`
  addresses) shown as clean text with headings, lists, tables, pictures and links. An address bar
  that also searches (the web, or the offline encyclopedia), back, forward, reload, and
  "Original".
- **Source links open in the Browser** (shift-click for a tab of your own browser).
- **Select any text** to Explore it as a Primer page or Ask about it.
- **Ask** about the page you're reading, answered from its text.
- **Click through for me**: give a goal and the writing model follows the page's links, up to
  five pages, showing each step and why, then answers. It can only follow links the page offers.
- **Make a page** from the page you're reading, grounded in it alone.

### Security

- Pages are rebuilt as Primer's own Markdown; no third-party HTML, script, style or SVG is ever
  served from Primer's address. Text is escaped so a page can't inject links or pictures.
- Web pages and pictures go through the same public-address guard as web sources; pictures are
  checked from their bytes (PNG, JPEG, GIF, WebP) and served with `nosniff` and a null
  content-security policy.

### Not yet

- **Image from a selection** and **Image of the page** are deferred: use Make a page, then Image.

## v0.6.0: narrated stories (2026-11-03)

Primer can now read to you.

### Added

- **Listen** reads the page aloud in a window that highlights the passage being read. Long pages
  are read in parts, so the first part plays while the rest is voiced.
- **Story** retells the page as three to five short scenes, each with a title, a picture (made
  through the usual picture queue) and narration. The Story window plays scene by scene, waits
  for a scene that isn't ready yet and carries on when it is, and keeps the story's phrases
  clickable. **Again** writes a new story. Also on branch forks, and typed: "tell me a story
  about a dragon".
- **A voice engine** (`[voice]` in the tier): Liquid AI's LFM3-Audio-2B on llama.cpp's
  `llama-server` (`kind = "lfm3"`, local, on the CPU, managed by Primer), or any OpenAI-style
  speech endpoint (`kind = "openai"`). The Engines window shows the voice server and voice.
- **Speakable text**: Markdown and citations dropped; numbers, years, ordinals, centuries, money
  and common units spelled out (measured: the voice said "768" for "1068" written in digits).
- Stories and readings are saved with the page, with their audio, and appear in its thumbnail
  strip.

### Measured

LFM3-Audio-2B Q8_0 on the CPU of an RTX 2060 machine: a page's 85 s reading was ready in 98 s
(the first part after 16 s), and a four-scene story was written and narrated in 138 s. It slows
sharply when other programs keep the CPU busy (`timeout_s` under `[voice]` allows for that).

## v0.5.0: the map lens (2026-11-03)

The places a page names can now be pinned on a map, offline.

### Added

- **Map** on any page, and on a branch fork for one phrase, opens a **Map window**: the places
  the page names, pinned on a dark world map, with a list saying what each one is. Click a pin or
  a place to open its page. **Again** finds them afresh.
- **Typed map requests**: "show me Bodiam Castle on a map", "map of Normandy", "X on a map".
- **An offline world map**: a Protomaps PMTiles file (OpenStreetMap data) read and served by
  Primer, drawn with MapLibre GL. No tile server is used.
- **`tools/map_extract.py`** makes the map file from Protomaps' public build: the world to a
  chosen zoom (zoom 9 is about 1.6 GB), optionally with one region in more detail. Standard
  library only, resumable.
- **Places located from Wikipedia, never guessed**: the offline encyclopedia first (using the
  place's description to pick the right one of several same-named articles), then, when web
  search is on, Wikipedia and Wikidata online. Lookups are cached.
- **Maps are saved with the page** and appear in its thumbnail strip.

### Changed

- The shipped tiers turn LFM3's thinking off for the fast model
  (`chat_template_kwargs = { enable_thinking = false }`). With thinking on, LFM3 spent its whole
  budget thinking and Primer's short jobs came back empty. LFM3-1.7B with thinking off writes far
  better branch cards and highlights than LFM2.5-1.2B did.

### Measured

A page's map in about 20 seconds (LFM3-1.7B on the CPU finding the places, the two Wikipedia
ZIMs locating them); one place in about 9 seconds.

## v0.4.0: grounding (2026-11-03)

Pages can now be written from sources: an offline copy of Wikipedia, or the live web.

### Added

- **Three source modes**: storyteller (the model's own telling), **offline reference** (Kiwix
  ZIM files of Wikipedia, served by `kiwix-serve`) and **web** (a search, then the top pages).
- **Citations and sources.** Grounded pages cite their sources as small numbered links, and a
  Sources list closes the page with each article's title and where it came from. Links open in
  a new tab.
- **Source badge and "Rewrite from".** The badge says what a page was grounded in
  (`offline reference · 3 sources`) and opens a menu to rewrite the page from another source.
  Older versions are kept.
- **Default source**, chosen in the Engines window (Modes → Source) and remembered.
- **Reference server.** Primer starts and stops `kiwix-serve` (`[reference]` in the tier) and
  shows it in the Engines window. An external `kiwix-serve` works too.
- **Web search engines**: DuckDuckGo (default, no key), SearXNG or Brave (`[web]` in the tier),
  or off.
- **Honest fallback.** When no source is found or the backend is down, the page is written from
  the model's own knowledge, recorded as storyteller, and the reader says why.
- **Extra request fields per engine** (`[text.extra]`), for example to turn off a model's
  thinking: `chat_template_kwargs = { enable_thinking = false }`.

### Security

- Web pages are fetched only from public addresses (`localhost`, private and link-local
  addresses are refused, also after a redirect), with size, type and time limits.
- Fetched text is given to the model as reference material, never as instructions, and is never
  shown as HTML. Source links are only ever `http` or `https`.
- `kiwix-serve` listens on `127.0.0.1` only, and its pages are never served from Primer's own
  address.

### Measured

With gemma-4-E4B on an RTX 2060: an offline-reference page in about 9 s (0.3 s to find its
sources), a web page in about 15 s.

## v0.3.0: video (2026-11-02)

Primer can now make short clips of what you are reading.

### Added

- **Video from any page or phrase.** Use the Video button on a page or a branch fork, or type a
  request such as "animate a trebuchet firing", "make a video of a castle siege" or "film the
  drawbridge rising". The fast model writes a prompt that describes the motion and the camera.
- **Animate.** A finished picture can be brought to life: Animate in the Image window uses it as
  the first frame of a clip.
- **Video window.** Live progress, then the clip playing in a loop, with its prompt, model,
  settings, length and seed. **Cancel**, **Again**, **Retry** and **Edit & run**, as for pictures.
- **Video thumbnails** on the page beside the pictures.
- **Five video model families** through the same stable-diffusion.cpp `sd-server`: Wan 2.2 TI2V
  5B, Wan 2.2 T2V A14B and I2V A14B (two-stage), LTX-2.3 distilled and LTX-2.3 dev.
- **LoRAs per model**, including LightX2V's 4-step LoRAs for Wan 2.2 A14B, with a separate stage
  (high noise, low noise or both) for two-stage models.
- **Models window** (was Image models): Pictures and Videos, each with its own default, video
  settings (frames, FPS, high-noise steps and CFG, MoE boundary) and the clip length.
- **Scan finds video models**, read from their file headers: Wan 5B, T2V and I2V are told apart
  by the model itself, encoders shared between models are found, and LoRAs beside a model are
  attached with the few-step settings they need.

### Changed

- Pictures and clips share one queue and one GPU. Auto GPU mode pauses the writing model for
  either.
- Media files are served with HTTP range requests, so clips can be scrubbed.

## v0.2.0: images (2026-11-02)

Primer can now draw what you are reading.

### Added

- **Pictures from any page or phrase.** Use the Image button on a page or a branch fork, or type
  a request such as "draw a trebuchet at dawn". The fast model writes the image prompt from the
  page, and the picture renders in the background while you keep reading.
- **Image window.** Live step-by-step progress with a preview, then the picture with its prompt,
  model and settings. **Cancel**, **Again** (a new random seed), **Retry**, and **Edit & run** to
  change the prompt, model or settings. Click a word of the prompt to explore it as a page.
- **Thumbnails** of finished and failed pictures at the bottom of the page they belong to.
- **stable-diffusion.cpp `sd-server`**, started and stopped by Primer, with three model families:
  SDXL, Qwen-Image 2.1 and MOSS-Image.
- **Image models window.** Scan a folder to find complete models by reading their file headers,
  add and edit models by hand with settings per model, and choose the default.
- **Auto GPU sharing.** While a picture is drawn, Primer pauses the writing model on the GPU, then
  brings it back. A page asked for in the meantime waits its turn. Manual mode leaves your servers
  alone. LM Studio tiers unload their models for the picture (`pause = ["lms", "unload", "--all"]`).
- **Reproducible pictures.** Every picture records the seed it used.
- **Crash cleanup.** Model servers Primer launched end with it, and leftovers from a crash are
  stopped on the next start. A process is never stopped just because it holds a port.
- **Docs.** A how-to guide ([docs/USING.md](docs/USING.md)), screenshots, and an Images section in
  [docs/RUNNING.md](docs/RUNNING.md).

### Changed

- Local model servers on `127.0.0.1` / `localhost` now bypass any system HTTP proxy. Hosted tiers
  still use it.
- The first image model you add becomes the default. Deleting the default falls back to another.

### Fixed

- Refusals (403/415) now read the request body first, so Windows no longer resets the connection
  before the client sees the error.
- A tier that names the image engine kind (`sdserver`) for text is refused with a clear message.
- The image model list is written under a lock, and a corrupt `var/images.json` is never silently
  replaced with an empty list.

### Measured

On an RTX 2060 (12 GB), 1024x1024 and 20 steps:

| Model | Time |
| --- | --- |
| SDXL | about 40 s per image once loaded |
| Qwen-Image 2.1 Q4_K_M | about 4 minutes |
| MOSS-Image SFT | about 4.5 minutes including the load |

## v0.1.0: pages and branching

- Type any topic and Primer writes a page about it. A fast model marks the phrases worth following.
- Click a phrase to branch: in this context, general, or sideways, with titles guessed ahead.
- Back and forward, Deeper, Simpler and Regenerate (old versions are kept).
- Pages are cached in a concept graph. The map shows where you've been.
- Tiers (Light, Hybrid, Workstation, Hosted) for LM Studio, llama-server, the Kairos sp-daemon or a
  hosted API. Primer starts and stops the servers it manages.
- A desktop of movable, resizable windows in the browser, using the Kairos design language.
- Listens on `127.0.0.1` only and answers only its own page.
