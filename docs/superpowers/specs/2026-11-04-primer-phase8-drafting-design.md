# Primer Phase 8: thinking ahead (speculative drafting) — Design

Date: 2026-11-04 · Builds on: [the main design](2026-11-01-illustrated-primer-design.md) §2
("speculatively drafting the most likely next page is a desirable follow-on") and
[the roadmap](2026-11-04-primer-roadmap.md) Phase 8 · Status: Claude's design; the user chose this
phase next.

## 1. Intent

A page takes a minute or so to write on a mid-range GPU. While someone reads, the writing model is
idle. Phase 8 uses that time to write the page the reader is most likely to open next, so that
when they click it, it is already there. It must never slow anything the reader actually asked
for.

Success: open "Warwick Castle", read for a minute, click the first underlined phrase's "In this
context" card: the page appears at once, with no streaming. Meanwhile, asking for a picture, or
typing a new topic, starts exactly as fast as it does today.

## 2. Goals

- **Draft the likely next page** after a page has been shown and its first branch cards exist:
  the first highlighted phrase's **In this context** card, then (at depth 2) that phrase's
  **General** card. Drafts are written exactly as the real page would be (same prompt, same
  source mode, same grounding).
- **Never in the way**: a draft runs only when the GPU gate is completely idle (nothing writing,
  no picture or video, nothing waiting), and steps aside the moment anything else asks for the
  gate: it stops generating and releases its turn within about a second. A cancelled draft is
  thrown away, and may be tried again later when the GPU is idle again.
- **Instant when right**: opening a page that has a finished draft promotes it to the page's
  first version and shows it at once (one `delta` with the whole text, `cached: false`,
  `drafted: true`), then highlights as usual.
- **Adopted when half-done**: opening the page a draft is *still writing* takes the draft over:
  the reader sees the text written so far at once and the rest as it streams; the draft is no
  longer pre-emptible.
- **Invisible otherwise**: a draft never appears in the map of where you've been, the page list
  or history until it is opened (no edge is recorded; drafts live in their own table).
- **A setting**: **Thinking ahead**: off / one page / two pages (default: one page), in the
  Engines window, saved in `var/settings.json`.
- **Visible**: Engines shows drafts written, used, discarded and the hit rate; branch cards whose
  page is drafted show a small "ready" mark.

## 3. Non-goals

- No drafting of pictures, video, maps or stories.
- No ranking model beyond "the first highlighted phrase" (highlights come back roughly in reading
  importance). Learning which cards a reader picks belongs to Phase 10's reader model.
- No drafting while auto GPU mode has the text paused for pictures.
- No drafting for Deeper/Simpler/Regenerate.

## 4. Decisions (Claude's calls)

### 4.1 The gate

`GpuGate.background()` returns a turn that is taken only if no turn is held or waiting
(`try_enter` semantics, never queued), counts as a page holder while held (so pictures still wait
for it, but it is told to stop), and exposes `preempted() -> bool`: true once any page or picture
is waiting. Real requests that arrive while a draft holds the gate wait at most the time it takes
the draft to notice (it checks between streamed pieces, and a 1 s watchdog closes the stream if
pieces stall).

### 4.2 Drafts

- Table `drafts(node_id, depth, source_mode, markdown, sources_json, status, created_at)`;
  statuses `writing`, `done`. Unfinished drafts are deleted on start. At most 30 kept (oldest
  dropped).
- A `Drafter` with one worker thread. After `PageService.open` finishes a page and its first
  phrase's cards are computed (the existing prefetch worker), it offers candidates. The worker
  drafts when the gate is idle and the setting allows, skipping nodes that already have a version
  or a finished draft for the current source mode.
- Drafting uses the same code path as `PageService` writing a page (grounding included), with a
  cancel check between streamed pieces. Grounded drafts fetch sources as usual (offline: free; web:
  a few seconds of network).
- A draft creates the target node with `graph.node_for` (no edge).

### 4.3 Opening a drafted page

`PageService.open` (not cached, normal depth, no regenerate):
1. Finished draft for this node and the current source mode → `add_version` from it (with its
   sources), delete the draft, emit `delta` (all text) and `done` with `drafted: true`.
2. Draft in progress for this node → adopt: emit the text so far, then each new piece until it
   finishes; then the same as 1.
3. Otherwise as today.
A finished draft for a different source mode is ignored (and dropped).

### 4.4 API and UI

- `/v1/status` adds `thinking_ahead: {setting, drafting: node_title|null, written, used,
  discarded, hit_rate}`; `POST /v1/thinking-ahead {setting: 0|1|2}`.
- `GET /v1/branches` adds `ready: true` on cards whose page has a finished draft.
- Engines: a **Thinking ahead** row (select and the numbers). Branch cards: a small "ready" mark
  (kit token colour, a glyph, no emoji).

## 5. Risks

- **GPU contention in manual mode**: there the gate only tracks state; a draft can run beside a
  picture. Drafting is therefore off in manual mode unless the setting is chosen explicitly
  (default stays one page only in auto mode).
- **Wasted work**: a miss costs GPU time and power while idle; the hit rate makes it visible.
- **LM Studio / llama-server slots**: cancelling closes the HTTP stream, which stops generation.

## 6. Left to the builder

The cancel/watchdog mechanics, how the worker learns that the first cards exist, and test
structure.
