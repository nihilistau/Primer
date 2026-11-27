# Primer roadmap after v0.7

Date: 2026-11-04 · Follows the seven phases of the [main design](2026-11-01-illustrated-primer-design.md)
(all shipped: pages, pictures, video, grounding, map, narrated stories, reader browser).

## What Primer has become

An offline interactive encyclopedia, in the spirit of Encarta (1993: articles, pictures, clips, an
atlas, audio, and MindMaze, a trivia quest through a castle). The difference is that this one
writes itself around the reader's curiosity: the encyclopedia is Wikipedia, the atlas is
OpenStreetMap, and the media is made on demand by local models, with some light-hearted fun.

The ideas below came from a conversation about that framing. They are ordered so that each phase
makes the next ones better, and so that Primer becomes easy to set up for other people early.

## Phase 8: thinking ahead (speculative drafting)

While someone reads, draft the most likely next page so that clicking it is instant. The spec
already named this as a follow-on to the branch-card prefetch.

- Pick the candidate from what the reader is likely to do next: the top-ranked phrase's
  "in this context" card first, then its "general" card.
- Draft only when the writing model is idle: no page being written, no picture or video job, no
  story being written, and never during auto GPU mode's image turns. Cancel the draft the moment
  the reader asks for something else, so it never delays a real request.
- Keep drafts as unseen versions; a click promotes the draft (instant), anything else discards or
  keeps it for later. Show hit rates in Engines.
- Setting: off / one page ahead / two pages ahead.

Biggest experience win for the least new surface.

## Phase 9: the setup centre (first-run window)

The remaining barrier to "other people with other hardware": setup means hunting for a dozen
files. A **Setup** window turns README's "What you need" into one screen.

- **Bundles per tier**, from the hardware detection Primer already has, e.g. "Hybrid, 12 GB:
  gemma-4-E4B, LFM3-1.7B, Qwen-Image, LFM3-Audio, top-50k Wikipedia, world map at zoom 9, about
  28 GB". The total against free disk space; each item expandable to swap a quant or skip it.
- **Catalogues that already exist**: Hugging Face's API (model cards, file lists, sizes, quants,
  SHA-256), Kiwix's OPDS catalogue (ZIMs by language, size and date), GitHub releases for
  llama.cpp, stable-diffusion.cpp and kiwix-tools (CUDA, Vulkan or CPU builds chosen from the
  hardware), and `map_extract.py` with a zoom slider and size estimate.
- **Mechanics**: resumable downloads with range requests, hashes verified against what the
  publisher lists, progress through the job/poller pattern, and when a download finishes, the path
  written into the tier and the Models scan run, so there is no config editing.
- **Moving models**: offer to move models from slow or external drives to an internal one (the
  author's picture models on an external drive broke Phase 6's live pictures).
- **Updates**: newer ZIMs and quants, offered, never automatic.
- **Security**: an allowlist of download hosts (Hugging Face, Kiwix, GitHub releases, Protomaps);
  pickle formats still refused; executables shown with source, version and hash, and run only
  after an explicit confirm; each model's licence shown from its card and acknowledged (Gemma's
  terms differ from Apache's); gated repos via a Hugging Face token in an `api_key_file`.

## Phase 10: the Primer knows you, and asks back

The Diamond Age Primer shaped itself around Nell and taught by questioning.

- **A reader model**: today adaptation is a list of recent titles. Primer already has richer
  signals: Deeper and Simpler show level, which branches a reader favours shows their curiosity
  style, time on a page shows engagement. The fast model keeps a compact "what this reader knows
  and how they like it" summary that page prompts use. Visible and editable in a window; can be
  reset.
- **Asks back**: a "Test me" button turns visited pages into a few Socratic questions; spaced
  repetition brings back things from the trail; answers feed the reader model.
- **The fun part**: a MindMaze-style quest, rooms whose doors open with questions drawn from what
  you have read.

## Phase 11: the continuing story

One ongoing tale with a recurring character whose episodes follow what the reader explores: read
about trebuchets and the next chapter has a siege; read about Gilgamesh and the character meets a
king searching for immortality. Builds on the Story lens and narration; the new part is persistent
story state (characters, places, what has happened) and choosing the next chapter from the trail.

## Phase 12: more lenses — timeline and diagrams

- **Timeline**: the fast model extracts dates, grounded from the ZIMs where it can, pinned on a
  timeline where every event opens a page. Cheap, given the map's plumbing, and natural for
  history.
- **Diagrams**: for "how does a trebuchet work", an SVG (or Mermaid) diagram from the writing
  model: faster than image generation and better at mechanisms, which image models get wrong.
  Rendered safely (sanitised SVG, no scripts), every label clickable.

## Phase 13: your book

"Your wandering becomes a book" is true inside the app; make it something to share: export the
trail as an EPUB (standard library: a zip of XHTML) and a single-file HTML, with pages, pictures,
maps as images, and the trail as the table of contents.

## Phase 14: the driven browser

Agreed in Phase 7: a Primer-launched, isolated Chrome shown as live screenshots, for sites that
need scripts or a login, with the model able to see the page and click for you.
Design: [2026-11-27-primer-phase14-driven-browser-design.md](2026-11-27-primer-phase14-driven-browser-design.md).

## Phase 15: Kairos as the front door

The companion talks to you and drives Primer ("show me Bodiam Castle", "tell me a story about
that"), with Primer as one of its tools. Primer's side only: a small, documented tool API (pages,
lenses, the trail) that Kairos can call on `127.0.0.1`. Kairos itself is never modified from this
repo.

## Phase 16: the bound book

Hold the book inside Primer: a book drawn in 3D on the desk, with a cover, pages that curl and
turn when you drag them, two-page spreads and a back cover, like leafing through a printed
magazine. Open any book Primer has made, or read the live trail as a book, as another way to read.
Citations and contents still turn to their pages. CSS 3D, with no new dependencies; pages are
real text, flowed into fixed page sizes in the browser from Phase 13's safe book blocks. Design:
[2026-11-15-primer-phase16-bound-book-design.md](2026-11-15-primer-phase16-bound-book-design.md).

## Phase 17: the leftovers

Every small item parked in Phases 11 to 16, gathered into one phase as the user asked
(2026-11-27). Each comes from that phase's notes.

**Done in v0.17.0 (2026-11-27).** Design:
[2026-11-27-primer-phase17-leftovers-design.md](2026-11-27-primer-phase17-leftovers-design.md);
rulings and live run: [../notes/2026-11-27-phase17-rulings.md](../notes/2026-11-27-phase17-rulings.md).
The DNS-rebinding gap is closed by a guarding proxy, and WebSockets work in live pages. The few
minor items still open after this phase are listed in those notes.

- **The bound book (Phase 16):**
  - The live announcement can still say "of about N" after the count is exact.
  - The bar's toolbar has no arrow-key movement between its buttons.
  - No component tests cover the book window's key and turn wiring.
  - A key pressed while a long book is laid out again can be undone when the place lands.
  - "Open in the Browser?" for web links was only checked against a stub.
  - Double-click can't select a word in a page's outer third.
  - A click is swallowed for 400 ms after a lost pointer capture.
- **Your book (Phase 13):**
  - A chapter shows every finished map (Warwick Castle has 5 plates); keep only the latest per
    phrase.
  - A failed status read during a job removes the book's files.
  - A heading indented 4 or more spaces is read as a heading, not code.
- **Timelines and diagrams (Phase 12):**
  - The 1st-millennium-CE axis bucket is too squashed to see.
  - Choose the final date list better: the last list, unless its labels are a subset of an
    earlier, fuller one.
  - The regnal-number rule accepts the word "I".
  - The window mount tests are missing.
  - SVG `use` and markers are dropped from diagrams.
- **The continuing story (Phase 11):**
  - Chapter 1's look-back can reuse pages another tale used that day.
  - Paraphrased repeats in picture prompts aren't removed.
  - A crash between a chapter's story and its row leaves an orphan story.
- **The driven browser (Phase 14):**
  - Close the DNS-rebinding gap, perhaps with a local guarding proxy, which could also allow safe
    public WebSockets.
  - A service worker whose set-up fails can run briefly without the socket lock before it's
    closed.
  - Worker set-up can hold a DevTools thread for up to 9 s, and slow lookups hold DNS pool slots.
  - Resizing has no lock (the UI's debounce prevents a pile-up).
  - The orphan sweep closes a window outside its lock.
  - A Make-a-page retry after its live window closes fails rather than falling back to the
    address.
  - Ask's live address skips `parse_address` cleaning.
  - A stalled oversize post can hold a thread for 2 s.
  - Test nits: sleep-based ordering in one WebSocket test, and an unasserted server error.
  - A changed `[browser] path` applies only after a restart, because the browser found is
    cached.
  - The live pools' threads are joined at exit, so a stuck lookup or worker set-up can hold
    up Primer's stop.
  - One image-service test (a server dying mid-job) is timing-sensitive under load.
  - The press's visibility check doesn't catch `filter: opacity(0)`, `clip-path`, `mask` or a near-zero
    `transform: scale`; this affects only what the reader saw, not where the click lands.

## Possible future: a native client (GPUI)

Not scheduled. Primer could gain a native desktop window drawn with GPUI, the Rust UI framework
from the Zed editor, beside the web UI rather than instead of it.

- **A spike settled the basics** (2026-11-21). A GPUI window read the running server with no
  server change: the trail, page text with clickable phrases and `[n]` citations, pictures and
  sources, in one file of about 500 lines. A page opened in 2–3 ms and its pictures in about 55 ms,
  using about 88 MB of memory. GPUI has clickable text ranges built in. The first build took about
  8 minutes; later builds took seconds.
- **The server stays as it is.** Reading needs nothing new. Writing (asking for a page, making
  media) needs a way in for a client that isn't a browser, such as a local token. The localhost
  guard trusts browser headers today.
- **What would cost the most:**
  - there is no HTML in GPUI, so the bound book's blocks need their own renderer;
  - maps need a Rust renderer, or images drawn by the server;
  - full-colour SVG diagrams and plates need to be turned into images first;
  - the book's 3D page curl needs custom drawing;
  - video and narration playback need their own Rust libraries;
  - screen-reader support has to be checked before committing.
- **A sensible order, if taken up:**
  1. the Reader and trail, with a token for writes;
  2. the Book window and the simpler windows;
  3. maps, diagrams and the bound book last.
- **A cheaper alternative** for a native window only: wrap the existing web UI (WebView2, for
  example with Tauri). It keeps everything, and takes about a day.
