# Primer Phase 11: the continuing story — Design

Date: 2026-11-09 · Builds on: [the roadmap](2026-11-04-primer-roadmap.md) Phase 11, the Story
lens (Phase 6) and the reader model (Phase 10) · Status: Claude's design; the user asked to
continue with the next phase.

## 1. Intent

Nell's Primer told one long story whose heroine lived through what Nell was learning. Primer's
Story lens retells one page as a short illustrated, narrated story, and then forgets it. Phase 11
adds **the Tale**: one ongoing story with a recurring hero, whose next chapter is built from what
the reader has explored since the last one. Read about trebuchets and Greek fire, and the next
chapter has a siege by sea; read about Gilgamesh, and the hero meets a king searching for
immortality.

Success: start a tale ("a girl called Nell who builds things", or let Primer invent one), read
three or four pages, press **Next chapter**: a chapter of 3–5 scenes arrives that continues from
the last one (same hero, same companions, the open threads remembered), weaves in what those
pages taught with the facts kept true, is illustrated with the hero looking the same from
picture to picture, and is read aloud with the same player as the Story lens.

## 2. Goals

- **A tale** has a **story bible** the reader can see and edit: the hero (name, a one-line
  description, a fixed *look* used for every picture), companions, places, things carried, open
  threads, and a one-paragraph summary of each chapter so far. One tale is current; older tales
  are kept and can be reopened.
- **Starting a tale**: the reader types a seed ("a girl called Nell who builds things") or asks
  Primer to invent one; the reader model's notes (Phase 10, e.g. "I'm 12") set the tone and age.
  The writing model turns the seed into the first bible.
- **Next chapter** draws on the pages read since the last chapter: up to three, chosen by time
  spent and how recently (the reader can untick any, or add a page from the trail). The Tale
  window shows which pages the next chapter would use and suggests one when three new pages are
  waiting (a quiet mark on the Tale dock button).
- **Writing a chapter**: the writing model gets the bible, the last chapter's summary, and short
  excerpts of the chosen pages (≤ 2500 characters each, framed as source material). It writes 3–5
  scenes in the Story format, keeping the pages' facts true and continuing the threads. Then it
  returns a **bible update** (new companions, places, things, threads opened or closed, this
  chapter's summary) as JSON, which is merged into the bible.
- **Illustration and narration** reuse the Story pipeline: each scene's picture prompt starts
  with the hero's *look* (and a named companion's look when they appear), so the hero is
  recognisable across chapters; narration uses the tale's voice.
- **A Tale window** (dock: the journal glyph): the current tale's chapters (each opens in the
  Story player), the bible (editable: name, description, look, and removing a thread or place),
  **Next chapter** with the pages it will use, **Start a new tale**, and the list of older tales.
- Chapters are stored as stories (kind `chapter`), so the Story window, its player and its
  highlights work unchanged; a chapter links to the pages it drew on, and those pages show the
  chapter in their stories list.

## 3. Safety and privacy

- All local. Page excerpts are framed as source material, never instructions; the reader's seed
  and bible edits are framed as the reader's wishes inside the prompt.
- The bible update is parsed strictly (known fields, short strings, at most 12 entries per list);
  anything else is ignored, so a page can't inject instructions into future chapters' bibles.
- Tone: for a curious reader, warm, with peril that resolves; the reader's notes decide the age.

## 4. Decisions (Claude's calls)

- **Writing model** for the bible, chapters and the update (quality, and it's a real request at
  the GPU gate); highlights stay on the fast model as for stories.
- **Chapters are stories**: `stories.kind = 'chapter'`, `node_id` = the main page drawn on; a new
  `tale_chapters(tale_id, n, story_id, pages_json, summary)` table orders them.
- **Two model calls per chapter** (scenes, then the bible update), not one combined JSON: the
  scenes stream well as prose, and a failed update leaves a good chapter (the bible just doesn't
  change; the window says so and offers to retry the update).
- **Look consistency** is prompt-only (no reference-image conditioning): the hero's look is a
  fixed phrase of ≤ 200 characters at the start of every scene's picture prompt.
- **Material choice**: pages opened since the last chapter, ranked by dwell seconds, then opens,
  then recency; pages already used by a chapter are skipped unless the reader adds them.

## 5. Non-goals

- No branching choose-your-own-adventure (the reader's branching is in the pages).
- No reference-image or LoRA character consistency.
- No sharing or exporting tales (Phase 13 exports books).

## 6. Left to the builder

Prompt wording, the bible JSON shape beyond the fields above, window layout, and test structure.
