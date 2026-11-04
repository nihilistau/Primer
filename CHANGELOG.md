# Changelog

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
