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

## Phase 15: Kairos as the front door

The companion talks to you and drives Primer ("show me Bodiam Castle", "tell me a story about
that"), with Primer as one of its tools. Primer's side only: a small, documented tool API (pages,
lenses, the trail) that Kairos can call on `127.0.0.1`. Kairos itself is never modified from this
repo.
