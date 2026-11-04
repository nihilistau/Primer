# Illustrated Primer — Design

Date: 2026-11-01 · Author: nihilistau (with Claude) · Status: draft for approval

## 1. What this is, and why

A local-first, model-driven explorer that feels like an **offline interactive Wikipedia grown out of the LLM**. You start from a loose interest ("castles") and follow curiosity wherever it goes: a page on trebuchets, an illustrated breakdown of siege-engine physics, a 5-second clip of a catapult firing, a map of real castles, a narrated story about a castle with pictures. Every piece of text is clickable, so you can endlessly click through concepts, and any concept can be opened through a different **lens** (text page, image, video, map, narrated story, live web page) in its own resizable window.

It's an extension of "just chatting with the LLM" into something navigable like a website: back, forward, follow a link, or type normally at any time.

Success is losing an hour and coming out knowing more than you meant to. The project is meant to be iterated on, not one-shot. The first version should get to "real thing I can use and change" quickly.

It is also meant for **other people with other hardware**. Every engine and capability is a choice, with sensible suggested tiers, not hard-wired to the author's machine.

## 2. Goals

- **Pages as the anchor.** Each concept is a page written by the model, shown in a reader window. Back/forward history works like a browser. A typing box is always available for normal prompting.
- **Click-to-branch (Option A, "branching lines").** Clicking a highlighted word or phrase lights it up and grows curved lines out of it to a few destination cards:
  - **In this context** (amber accent): the concept as it relates to the current page ("Why the trebuchet's counterweight swings").
  - **General**: the concept on its own ("Counterweight: the principle, from cranes to lifts").
  - **Sideways**: a related concept worth jumping to ("Potential energy").
  - Plus actions: **Image**, **Video**, **Ask about it** (puts the phrase into the prompt box so you can phrase your own question).
  Clicking a card opens that page. Clicking elsewhere dismisses the fork. A mockup lives in `.superpowers/brainstorm/*/content/branch-popup.html` (option A).
- **Thinking ahead.** A small, fast model reads each page as it appears. It decides which phrases are worth branching from (these get the dashed underline) and pre-writes their branch cards, so the fork opens instantly. The larger model writes the pages themselves. Speculatively drafting the most likely next page is a desirable follow-on.
- **Lenses open in their own resizable windows**, on a desktop like Kairos's. Lens order of priority (agreed): **images → video → map → narrated stories → browser**.
- **Asynchronous generation.** Asking for an image or video queues a job and immediately opens its window with a placeholder. You keep reading and clicking while it renders. Video is always a background job.
- **Concept graph, not just history.** Every visited page records which phrase and which branch card led to it, and what it spawned (pages and media). Breadcrumbs plus a map/tree view let you jump back to any branch.
- **Cache everything.** Pages, images, videos and audio are stored per node, so revisiting is instant and the explored book builds up into something you can browse later. Revisiting shows what you saw before. **Go deeper** and **Simpler** controls regenerate on request; previous versions are kept.
- **Adapts to the reader.** Exploration history (what you've already visited) is fed into page generation so depth and examples build on what you've seen rather than repeating it.
- **Everything is a choice.** Engines, source modes and tiers are selectable (§4).

## 3. Non-goals (for now)

- Not a fork of, or a mode inside, Kairos. Kairos stays untouched; this is a sibling project.
- Not a multi-user or hosted service. Single user, local machine.
- No accounts, sync or sharing in the first versions.
- No attempt to make cheap hardware do expensive things. Tiers make the trade-offs visible instead.

## 4. Engines, tiers and source modes (the user's decisions)

### 4.1 Selectable engines

- **Text (writing) model**: any of LM Studio, llama-server (llama.cpp), the custom **sp-daemon** (Kairos's engine, `kairos-engine`), or a hosted API. This mirrors Kairos's split: `sp` vs any OpenAI-compatible `/v1/chat/completions` endpoint.
- **Fast model** (for highlights, branch cards and summaries): separately selectable. Expected defaults: LFM3 or a similar small CPU model.
- **Image, video and voice**: each selectable as local or hosted, per job type.
- **The Primer starts and stops the model servers itself** (LM Studio, llama-server, sp-daemon, the image runtime), with launch, health checks and clean shutdown, the way Kairos's server controller does. Connecting to an already-running external server remains an option.
- **GPU memory is managed automatically by default, with a manual mode.** On a card too small to hold everything (e.g. 12 GB with Gemma 26B and SDXL Turbo), asking for an image automatically unloads or offloads the text model, runs the image job, then reloads the text model. Manual mode leaves loading and unloading to the user. Either way, the UI shows what's occupying GPU memory and lets you load, unload or pin models. Making swaps faster (keeping weights in system RAM, etc.) is a later optimisation task, explicitly wanted.
- Any capability a configuration can't provide is shown as **unavailable with the reason**, not hidden (the pattern Kairos uses for backends).

### 4.2 Ability tiers (suggested presets, all editable)

| Tier | Text | Fast model | Images | Video | Voice |
|---|---|---|---|---|---|
| **Light** | small CPU model (LFM3-class) | same | optional / hosted | hosted or off | local small TTS |
| **Hybrid** (author's RTX 2060 12 GB) | Gemma 4 12B or 26B (LM Studio / llama-server / sp-daemon) | LFM3 on CPU | SDXL Turbo, loaded on demand | hosted (xAI) or slow local queue (LTX-Video) | LFM3 / hosted |
| **Workstation** (24–32 GB+) | larger local model | local | bigger image models resident | local (LTX-Video / Wan) | local |
| **Hosted** | cloud | cloud or local | cloud | cloud | cloud |

On first run the Primer detects hardware (GPU, VRAM, RAM) and **suggests** a tier. Tiers are plain, readable config files, like Kairos `profiles/*.toml`.

### 4.3 Source modes (grounding)

Selectable as a default and overridable per page:

- **Storyteller**: the model writes freely. Fast, fully offline, labelled as the model's telling.
- **Offline reference**: pages are grounded in a local Wikipedia copy. Claims link to the source article.
- **Web**: live search and fetch when online.

Every page shows a small badge saying which mode produced it. Modes can differ between pages (a narrated story in storyteller mode next to a sourced physics page).

## 5. Lenses (in build priority)

1. **Page** (text). Reader window, serif "model voice" type, highlighted branchable phrases, branching-lines fork, back/forward, go deeper/simpler, source badge.
2. **Image**. Generated from the clicked concept plus page context. The fast model writes the image prompt ("deciding what to illustrate"). Opens in a resizable window with a placeholder while queued.
3. **Video**. Short clips (≈5 s), always a background job. Resizable window, placeholder plus progress.
4. **Map**. A geographic map (Google-Maps-like) for place-based concepts, e.g. historical castles, with clickable pins that open pages.
5. **Narrated story**. Text-to-speech reading of a story-form page with accompanying images.
6. **Browser**. A real web browser window the model can see and act on: "what's on this page?", "make an image from this", and eventually "click through this for me". This is a project in itself and comes last.

Each lens window's text is itself clickable where it has text (captions, story text, map pin labels), so exploration never dead-ends.

## 6. Look and feel

Reuse Kairos's visual language and desktop:

- **Windows and desktop**: draggable, resizable (8 grips), maximise and minimise windows; dock; taskbar. Copied from `shannon-prime-kairos/ui/src/` (`windowManager.js`, `room/Dock.jsx`, `Folder.jsx`, `DeskIcons.jsx`, `desktopLayout.js`, `apps/panel.jsx`).
- **Design kit**: `ui/src/kit/` (tokens.css, kit.css, parts.jsx, icons.jsx, self-hosted fonts). Dark frosted panels, cyan accent `#46d9e6`, warm `#e8b64c` (used here for "in this context"), Inter for UI, **Source Serif 4 for page text**, JetBrains Mono for code. Hand-drawn glyph icons, no emoji.
- Copy from the **source** tree (`shannon-prime-kairos`), never write to the `Kairos` export.

## 7. Decisions made by Claude (marked for review)

These are my calls, not agreed with the user. They're easy to change.

- **Repo and location.** Standalone project at `the repo root`, its own git repo.
- **Architecture mirrors Kairos.** Python backend serving a React 18 + Vite UI on localhost. HTTP JSON for requests, **SSE** for streaming page text and job progress. It runs as a local web app opened in the browser, like Kairos.
- **Backend reuse by copying.** Copy standard-library pieces from Kairos where they fit (inference client and backends, `harness/skills/xai.py` for hosted image, video and TTS, `harness/voice/` for LFM3 and TTS fallbacks, search and research). Kairos code is copied and adapted, not imported, since much of it reads Kairos env vars and paths.
- **Unlike Kairos, the Primer may take third-party Python dependencies** where they clearly earn it (local image generation needs `diffusers`/`torch` or an external image server). Builder decides the exact local-image route (in-process diffusers vs an external server such as ComfyUI or stable-diffusion.cpp). Requirements: SDXL Turbo works on a 12 GB Turing card; load/unload is controllable.
- **Storage.** One SQLite file for the concept graph (nodes, edges, versions, jobs), with media files on disk under a cache folder, referenced by hash.
- **Page format.** The model writes Markdown. Branchable phrases are marked by the fast model *after* generation (spans over the rendered text), so the writing model's prompt stays simple and any model works.
- **Tier detection** uses `nvidia-smi` plus OS memory queries, and only suggests. It never auto-downloads models.
- **Offline Wikipedia format.** Kiwix ZIM files (widely available, single file, searchable). Builder may pick another if it's clearly better.
- **Page identity: the graph is a network, not a tree.** "General" and "Sideways" pages are keyed by concept, so reaching "Counterweight (general)" from trebuchets or from elevators opens the same cached page, with two incoming edges. "In this context" pages are keyed by (concept, parent page). Every arrival is still recorded as an edge, so breadcrumbs show the route you actually took.
- **Navigation.** Clicking a branch card replaces the page in the current reader window, like following a web link. Ctrl/middle-click opens it in a new reader window. Back and forward are per reader window, and the concept graph is global. Several reader windows can be open at once.
- **Typing box.** Typed input creates a new page node (the question becomes the page's topic), linked from the page you were on, so typed questions are part of the graph too.
- **Text backends.** Phase 1 ships both of Kairos's backends: the OpenAI-compatible client (LM Studio, llama-server, hosted) and the sp-daemon client, which has its own protocol (`harness/inference/backends/` in Kairos).
- **Kairos memory integration is out of v1.** Adaptation uses the Primer's own exploration history first. Reading Kairos `memory-okf` is a later option.

## 8. Phasing (proposal)

- **Phase 1, "the book works"**: desktop shell plus reader window, a pluggable text engine (OpenAI-compatible + sp-daemon), page generation with streaming, fast-model highlights and branch cards, branching-lines fork, back/forward, concept graph plus cache, typing box, storyteller source mode, a tier config file.
- **Phase 2, images**: job queue, image lens (local SDXL Turbo plus hosted), load/unload UI, fast-model image prompts.
- **Phase 3, video**: hosted plus local video as background jobs.
- **Phase 4, grounding**: offline reference (ZIM) and web source modes, source badges and links.
- **Phase 5, map lens.**
- **Phase 6, narrated stories** (TTS).
- **Phase 7, browser lens.**
- **Phases 8–15** (added 2026-11-04, after all seven shipped): thinking ahead, a setup centre, a reader model that asks back, a continuing story, timeline and diagram lenses, exporting the trail as a book, a driven browser, and Kairos as the front door. See [the roadmap](2026-11-04-primer-roadmap.md).
- Tier presets plus first-run hardware suggestion land with Phase 1 (config) and grow as lenses arrive. The graph/tree map view lands with or after Phase 1.

## 9. Left to the builder

- Exact prompts, the page length and structure, how many branch cards (≈3 plus actions is the target).
- How highlights are chosen and cached; when speculative next-page drafting kicks in.
- The local image and video runtime choice (within §7's constraints), and swap-speed tricks.
- Map tiles and data source for place-based concepts.
- Window default sizes and placement.
- Test approach.

## 10. Hardware reference (author)

RTX 2060, 12 GB VRAM (Turing: no fast bf16/FP8, so Flux is slow and SDXL Turbo is the practical local image model). sp-daemon serves Gemma-4-26B-A4B MoE.
