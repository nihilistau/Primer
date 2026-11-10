# Primer Phase 13: your book

Date: 2026-11-10 · Follows the [roadmap](2026-11-04-primer-roadmap.md#phase-13-your-book) and
v0.12.0.

## 1. Intent

"Your wandering becomes a book" is true inside the app; this phase makes it something to keep and
share. A reader picks part of their trail (or all of it), and Primer writes it out as a book:

- an **EPUB 3** for e-readers and phones;
- a **single HTML file** that opens in any browser and can be emailed.

Each page they read becomes a chapter, in the order they found it, with its pictures, its maps
drawn as images, its timeline and diagram, and its sources. The table of contents is the trail
itself: a chapter that branched from another sits under it.

The book has to work far from Primer: no links back to `127.0.0.1`, no scripts, nothing fetched.
It is made from what is already stored, so it needs no model and no GPU.

## 2. Goals

1. **A Book window** (a new dock button, "Book"). It offers:
   - **What goes in** (scope):
     - *Everything I've read*;
     - *A page and what branched from it*: pick the page from a searchable list of read pages;
     - *Since a date*: pages first opened on or after it.
   - The book's **title** (editable; default "My wanderings" for Everything, the page's title for
     a branch, "Since <date>" for a date) and an optional **By** line.
   - **Include** switches: pictures, maps, timelines, diagrams, stories (all on by default).
   - **Make the book**: a background job with progress ("Chapter 12 of 40…"), then
     **Download EPUB** and **Download HTML**. The browser downloads the file.
   - A preview count before making it: chapters, pictures, and an estimated size.
   - Earlier books are listed with their date and can be downloaded again or deleted.
2. **Chapters.**
   - One chapter per page (node) in scope that has a page version: the latest version's text.
   - Under the title, a line says how the reader got there: "From *phrase* in *Parent page*",
     linking to that chapter when it is in the book.
   - The page's Markdown is rendered to safe XHTML. Citations `[n]` become superscript links to
     the chapter's numbered **Sources** list, which gives each source's title and kind
     (Wikipedia, web); http(s) URLs appear as links, local Kiwix ones as text only.
   - A chapter whose phrase-page shares its parent's title gets the phrase as a subtitle.
3. **Order and contents.**
   - Chapters are in trail order: by the time the page was first arrived at (its first edge in),
     else its first version's time.
   - The table of contents (`nav.xhtml` in the EPUB, a list at the top of the HTML) nests each
     chapter under the chapter it branched from when that parent is in the book. Otherwise it
     sits at the top level.
   - Depth is capped at 6. Deeper chapters are listed at depth 6.
4. **Pictures.**
   - A page's finished images (kind `image`, status `done`) go after its text, oldest first, each
     with its phrase (or "The whole page") as the caption.
   - At most 6 per chapter, and 300 in the book (later ones are left out, and the book says so in
     its closing note).
   - Videos are not included. A chapter with a finished clip says "A video clip was made of this
     page in Primer."
5. **Maps as images.**
   - Each map (status `done`, with located places) becomes an SVG plate: land, water and country
     borders drawn from the tier's offline world map (PMTiles), framed to the places with a
     margin. Each place is a numbered pin, and a numbered list of places goes under it.
   - Tiles are decoded with the standard library (PMTiles directory plus Mapbox Vector Tile
     protobuf).
   - The frame uses one zoom level from 0 to 7, chosen so the frame needs at most 16 tiles.
   - The plate is at most 300 KB of SVG; geometry is simplified until it fits, and failing that
     the plate falls back to the plain version below.
   - With no map file in the tier, or when a tile can't be read, the plate is a plain one: pins
     on a latitude/longitude grid, with the note "Drawn without the world map".
6. **Timelines.**
   - The page's latest finished timeline is drawn as a vertical list (XHTML, no script): the
     date as written, then the label.
   - Approximate dates are marked "about"; spans show "to" and their end year.
7. **Diagrams.**
   - The page's latest finished diagram is embedded as inline SVG, cleaned again with the
     Phase 12 allowlist (`clean_svg`) at export time, with its caption.
   - The id prefix stays unique per diagram.
8. **Stories.**
   - Stories and Tale chapters made from pages in the book go in a **Stories** part after the
     chapters.
   - Each gets its title, scene titles and text, and scene pictures (counting towards the
     picture cap).
   - Narration audio is not included.
9. **A cover and a closing note.**
   - The cover page has the title, the By line, the date range of the reading, and the first
     picture in the book if there is one.
   - The closing note says how many pages and pictures the book has, what was left out (by the
     caps), and "Made with Primer, from what I read between <first> and <last>."
10. **Both formats from one model of the book.**
    - The EPUB is a zip whose first entry is `mimetype` (stored, uncompressed), then
      `META-INF/container.xml`, `OEBPS/content.opf` (EPUB 3, with dc:identifier as a urn:uuid,
      dc:title, dc:language "en", dc:creator from By, and dcterms:modified),
      `OEBPS/nav.xhtml`, one XHTML file per chapter, `OEBPS/style.css`, and the images under
      `OEBPS/images/`.
    - SVG plates and diagrams are inline, with the manifest item's `properties="svg"`.
    - The HTML is one file: the same chapters and CSS inline, with images as `data:` URIs.

## 3. Safety

- **All text from the database is escaped** for XML: titles, phrases, Markdown output, captions,
  labels, place names and source titles.
- **The Markdown renderer emits only** `h1`–`h4`, `p`, `em`, `strong`, `ul`, `ol`, `li`, `sup`, `a`
  (to `#` anchors within the book, or http(s) only) and `br`. Raw HTML in the Markdown is shown as
  text.
- **Diagram SVG is re-cleaned** with `clean_svg`. Map plates are generated by our own code from
  numbers only.
- **No external references in the book.** No scripts, remote images, fonts, iframes, forms or
  `127.0.0.1` URLs; http(s) source links are the only outbound links.
- **Files go only into `var/books/`**, with names built from a random id. The download name is a
  slug of the title (ASCII letters, digits and hyphens, at most 60 characters) with `.epub` or
  `.html`. Downloads are served with `Content-Disposition: attachment` and the right type
  (`application/epub+zip`, `text/html; charset=utf-8`). Ids are validated against
  `[0-9a-f]{16}`.
- **Media files are read only through the existing media-name check**: 64-hex names under the
  media folder, as the media route does.
- **Limits:**
  - 1000 chapters;
  - 300 pictures;
  - the EPUB and HTML each at most 400 MB, checked against the estimate before building;
    over that, the request is refused with a reason ("Too big: leave out pictures or choose
    less");
  - titles 120 characters and the By line 80.
- **Concurrency:** one book job at a time (409 while one is running). Jobs are recorded in the
  graph, and an unfinished job is failed on restart.

## 4. Decisions (Claude's calls)

- **The table of contents follows the first arrival edge.** A page reached twice is a chapter
  once, placed by its first arrival.
- **Timelines are a list, not a drawing.** A vertical list reads well on a phone e-reader and
  reflows. A horizontal axis does not.
- **Maps as SVG, not PNG.** There is no image library in the standard library, and SVG stays
  sharp. EPUB 3 readers support SVG. The plain grid fallback keeps a book possible with no map
  file.
- **PNG pictures go in as they are.** No re-encoding without a library. The size estimate and the
  pictures switch handle large books.
- **No audio or video** in the book, for size and reader support.
- **The book is built from the latest version of each page**, not every version.
- **Private data stays out:** the reader profile, notes, quiz items, quests and the Tale's bible
  are not in the book. Stories and Tale chapters are, because they are the reader's own creation.
  The Tale's hero is described only by what the chapters say.

## 5. Non-goals

- Printing or a PDF (a browser can print the HTML).
- Editing chapters before export.
- Sharing or uploading anywhere. The book is a local file the browser downloads.
- Fonts embedded in the book (the reader's defaults are used).
- epubcheck in the test suite, since it is a Java tool. Structural checks are tested instead (see
  §6).

## 6. Left to the builder

- CSS details (a readable serif body, a caption style, a plate frame). Light only, with no dark
  mode in the book.
- How the map plate projects and simplifies, within the limits above (Web Mercator for the tile
  frame is the natural choice).
- Exactly how the closing note words what was left out.
- **Tests must prove:**
  - The EPUB zip order, with `mimetype` stored first.
  - Every XHTML file parses as XML.
  - Every manifest item exists and every file is in the manifest.
  - The nav lists every chapter and nests by trail.
  - No `<script`, `127.0.0.1` or `javascript:` anywhere in the output.
  - Hostile Markdown, titles and SVG come out escaped or cleaned.
