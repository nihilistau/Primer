# Primer Phase 16: the bound book

Date: 2026-11-15. Follows v0.14.0 and the [roadmap](2026-11-04-primer-roadmap.md#phase-16-the-bound-book).
Status: **approved 2026-11-16**; built before Phase 14 at the user's choice. Ships as **v0.15.0**.

## 1. Intent

Phase 13 turns the trail into an EPUB and an HTML file to download. This phase lets the reader
**hold the book inside Primer**: a book drawn in 3D on the desk, with a cover, a spine, pages that
curl and turn when dragged, and a back cover. The reader leafs through what they read, the way the
user's reference images show a printed magazine:
- a cover with the title and the first picture;
- spreads of text and plates;
- a quiet back cover with a colophon;
- a small bar under the book, "‹ Pages 10–11 of 12 ›".

It is also an **alternative way to read**. Any book Primer has made can be opened as a book, and so
can the live trail ("my trail as a book"), with no file made first. Links inside it still work: a
citation turns to its source list, and a contents entry turns to its chapter.

## 2. What the reader sees

1. **Ways in:**
   - **Open as a book** beside Download EPUB and Download HTML in the Book window, for any finished
     book and each earlier book.
   - **Read my trail as a book** in the Book window. It builds the same book in memory from the
     current scope and include switches, with no files and no job.
   - A **Book view** toggle in the Reader header: the current page and what branched from it, as a
     book opened at that chapter.
2. **The book on the desk.** A window shows the closed book at a slight angle, with the cover lit
   and a soft shadow. Clicking it, or pressing Enter, opens it flat to the first spread.
3. **Turning pages.**
   - **Drag:** dragging a page corner or edge lifts it into a curl that follows the pointer.
     Releasing past the middle completes the turn; otherwise it falls back.
   - **Click or tap:** clicking the outer third of a page, or the ‹ › buttons, turns one leaf with
     a short animation (about 600 ms).
   - **Keyboard:** ← and → turn a leaf, Home and End go to the covers, and PgUp/PgDn go to the
     previous or next chapter.
   - **Page bar:** "Pages 10–11 of 48" below the book. Clicking the number opens a small "go to
     page or chapter" list.
   - **Thumbnails** (the eye-like button in the bar): a strip of spreads to jump to.
4. **Layout.**
   - A wide window shows **two-page spreads**, with the cover alone on the right and the back cover
     alone on the left. A narrow window, or a phone, shows **one page**, turning as a single leaf.
   - Pages keep a fixed proportion (A5, 1 : 1.414). Type is a readable serif. Plates and pictures
     sit wherever they fit, and are never split across a turn; one that won't fit heads the
     next page while the following text fills the gap. Only the covers stand alone; an odd
     inside page faces a blank page.
   - **Night paper by default** (the user's choice): dark pages with light ink, like the reference
     image's "Seed Pods & Roots" spread. A **Day paper** option gives light pages, as on paper. The
     choice is remembered.
5. **Inside the book.** It is the same book as Phase 13's model:
   - a cover with the title, the By line, the date range and the first picture;
   - the contents, following the trail;
   - each chapter: title, "From *phrase* in *Parent*", text, pictures, map plates, the timeline
     list and the diagram, then its sources;
   - the Stories part;
   - the closing note;
   - a back cover with the title small and "Made with Primer".
   Running heads give the book title on the left page and the chapter title on the right, with
   folios at the foot.
6. **Links.** A citation turns to the chapter's sources page. A contents entry turns to its
   chapter. An http(s) source link asks first: "Open in the Browser?" opens Primer's own Browser
   window, never a new tab. **Open this page in Primer** on any chapter opens that page in a
   Reader.
7. **Reduced motion.** With `prefers-reduced-motion`, pages cross-fade instead of curling, and the
   desk shadow doesn't animate.
8. **Sound** is optional and **off by default** (the user's choice): a soft paper turn, made in
   the browser with no file, switched on from the book's bar.

## 3. How it is built (Claude's calls)

- **No new dependencies.** The book is drawn with **CSS 3D transforms**:
  - `perspective`, `transform-style: preserve-3d`, and `rotateY` on each leaf around the spine;
  - two faces per leaf with `backface-visibility: hidden`.
  This is enough for a convincing hardback or magazine and keeps pages as real, selectable,
  accessible DOM text.
  - **The curl.** During a drag the lifting leaf is split into a few vertical strips, each rotated
    a little more than the one before, with a moving gradient for the light across the fold and a
    shadow cast on the page beneath.
  - **The animation** runs on `requestAnimationFrame` from the pointer position, with a spring for
    the release.
  - WebGL and three.js were considered and rejected: they need a dependency, and turn text into
    textures, which loses selection, find-in-page and screen readers.
- **Pagination happens in the browser.**
  - **Content.** The server gives the book as an ordered list of blocks:
    - a new route, `GET /v1/books/<id>/blocks`, for a made book;
    - `POST /v1/books/blocks {scope, include, title, by}` for the live trail.
    Each block is `{kind: cover|contents|chapter-head|para|heading|list|picture|plate|timeline|diagram|sources|story|closing|back, html, chapter}`. The HTML is built by the Phase 13 xhtml builders, so it is already escaped, allowlisted and cleaned. Map plates and diagrams are inline SVG; pictures are `/v1/media/<name>` URLs, not data: URIs, so nothing large is copied.
  - **Flowing pages.** The UI pours blocks into page-sized boxes, measuring each one in a hidden
    page-sized frame. A paragraph too long for the space left is split at a word boundary across
    pages. Pictures, plates, timelines and diagrams are never split: one that won't fit moves to the
    next page, and the gap is closed with the next text.
  - **Speed.** Layout runs in chunks on idle time, so the first spread shows at once. The page
    count fills in as it goes: "Pages 2–3 of about 40", then exact.
  - **Re-layout.** It happens on window resize (debounced), font size or theme change, and keeps the
    reader on the same chapter and paragraph.
- **Rendering only the pages near the reader.** Only the spreads within ±2 of the open spread are
  in the DOM, so a 1000-chapter book stays light.
- **Remembering the place.** The last page read is kept per book in `localStorage`, and the book
  reopens there.
- **Accessibility.**
  - Each page is a region labelled "Page 11", and the bar is a toolbar.
  - Keyboard turning moves focus to the new page.
  - Screen readers get the text in reading order. Turning announces "Pages 10–11 of 48, Chapter:
    Bodiam Castle".

## 4. Safety

- **Same HTML as the downloads.** The book shows only HTML from the Phase 13 builders: escaped
  text, the tag allowlist, re-cleaned diagram SVG, and plates built from numbers. The UI inserts
  it as HTML, which is the one place Primer does so, and only from those routes. It runs it
  through the existing client-side SVG and HTML check before inserting.
- **No outbound loads.** Media comes from Primer's own `/v1/media/` route through the 64-hex-digit
  name check.
- **Links never open a new tab.** External links only offer Primer's Browser window.
- **Limits.** `POST /v1/books/blocks` follows the Phase 13 limits: 1000 chapters, 300 pictures, a
  120-character title and an 80-character By line. It is read-only and makes no files.

## 5. Non-goals

- Editing the book's text.
- Print layout or a PDF; the HTML download already prints.
- Physically exact paper simulation, such as a full mesh curl or page thickness under the thumb.
  The curl is a convincing illusion.
- Kairos turning pages. That could come later through Phase 15's `lens`.

## 6. The user's answers (2026-11-16)

1. Night paper by default, with Day paper as the option.
2. The Book view toggle in the Reader header is in this phase.
3. The page-turn sound is in, off by default.

**Claude's call on made books.** The job that makes a book also saves its blocks
(`var/books/<id>.blocks.json`), so "Open as a book" shows exactly what was downloaded. A book made
before v0.15.0 has no saved blocks; it is rebuilt from its saved scope and switches, and says so
once: "Rebuilt from your trail as it is now".

## 7. Tests the plan should require

- **Pagination:** every block appears exactly once, in order. No picture, plate, timeline or
  diagram is split. A paragraph split across pages rejoins to its original text. Re-layout at a
  new size keeps the reader in the same chapter.
- **Turning:** the keyboard and buttons turn exactly one leaf. A drag released before the middle
  falls back. Home and End reach the covers. A narrow layout turns single pages.
- **Links:** a citation goes to its sources page; a contents entry goes to its chapter; an
  external link only offers the Browser window.
- **Safety:** the blocks route carries no `<script`, `javascript:` or event-handler attributes,
  even for hostile titles, Markdown and SVG.
- **Reduced motion:** pages cross-fade and nothing animates.
