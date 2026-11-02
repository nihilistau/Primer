# Primer Phase 5: the map lens — Design

Date: 2026-11-03 · Builds on: [the main design](2026-11-01-illustrated-primer-design.md) §5 (lens
4) and §9 ("map tiles and data source for place-based concepts" left to the builder) · Status:
Claude's design, written to the user's standing instruction to continue the phases.

## 1. Intent

A page about castles mentions Warwick, Kenilworth, Caernarfon and Bodiam. The map lens shows them
as pins on a real map. Clicking a pin opens that place's page, so the map is another way to keep
following curiosity. It works offline, like the rest of Primer: the map itself and the place
lookups come from files on the user's disk.

Success: on the Warwick Castle page, **Map** opens a window within seconds showing Warwick,
Kenilworth and the other places the page names, pinned in the right spots on a dark map with
labels. Clicking Kenilworth opens the Kenilworth Castle page. Pulling the network cable changes
nothing.

## 2. Goals

- **A Map button** in the reader header (beside Image and Video) opens a **Map window** for the
  page: the places it mentions, pinned.
- **Map on a branch fork** maps a single phrase (for "Warwick", one pin).
- **Typed requests**: "show X on a map", "map of X", "X on a map" open the page for X (as a typed
  page would) and its Map window.
- **Pins open pages.** Clicking a pin, or a place in the window's list, opens that place's page in
  a reader (as a **General** branch from the mapped page, so the concept graph records it).
  Shift/middle click opens a new reader, like branch cards.
- **The place list**: every place found, with what it is ("castle in Warwickshire"), clickable;
  places that couldn't be located are listed as such (never guessed onto the map).
- **Offline map**: a PMTiles vector basemap (Protomaps, OpenStreetMap data) on the user's disk,
  served by Primer, drawn with MapLibre GL in the dark Kairos look. Attribution is always shown.
- **Offline place lookup**: coordinates come from the offline Wikipedia (Phase 4's kiwix-serve).
  When the tier allows web search, Wikipedia's online API fills the gaps. Lookups are cached.
- **Maps are saved** with the page version, like pictures: reopening the Map window is instant.
  **Again** looks the places up anew.
- **Making the map file**: `tools/map_extract.py` cuts a world map (zooms 0–9, about 1.6 GB) or a
  region at higher zoom out of Protomaps' public planet build, using only the standard library.
- **Unavailable with the reason**: no `[map]` in the tier; the map file is missing; no way to look
  places up (no reference server and web search off).

## 3. Non-goals

- No routing, search box, geolocation of the user, or editing.
- No online tile servers (OpenStreetMap's tile servers forbid heavy use, and Primer is offline-first).
- No historical borders or time slider.
- No geocoding service besides Wikipedia (no Nominatim): a place without a Wikipedia article or
  coordinates is listed as not located.
- No model-guessed coordinates, ever.
- No change to other lenses. Pictures and clips are unaffected; the map is not a GPU job.

## 4. Decisions (Claude's calls)

### 4.1 The map file and how it is served

- **Format**: PMTiles v3 (one file, vector tiles, gzip). Primer reads it itself
  (`primer/maps/pmtiles.py`, stdlib) and serves `GET /v1/map/tiles/{z}/{x}/{y}.mvt` (gzip
  content-encoding, `application/vnd.mapbox-vector-tile`; 204 for an empty tile), plus
  `GET /v1/map/tilejson` with the file's bounds, zooms and attribution.
- **Assets**: glyphs and sprites from Protomaps' `basemaps-assets` (fonts are OFL; the repo
  pinned at a commit), in a folder the tier names, served read-only at `GET /v1/map/assets/...`:
  `.pbf`, `.json` and `.png` only, no path outside that folder.
- **Style**: built in the UI with `@protomaps/basemaps` (`layers("protomaps", namedFlavor("dark"),
  {lang: "en"})`), tuned to the kit's tokens where the style allows (background, water, labels).
- **MapLibre GL JS** draws it. It is loaded with a dynamic `import()` only when a Map window
  opens, so the main bundle doesn't grow.
- **Overzoom**: tiles exist to the file's max zoom (9 for the world file); MapLibre stretches them
  beyond, so a pin can still be zoomed to street level without new data. A region extract adds
  detail where wanted.
- **Tier config**:

  ```toml
  [map]
  pmtiles = "D:/Files/Reference/maps/world-z9-20261101.pmtiles"
  assets = "D:/Files/Reference/maps/basemaps-assets"
  ```

### 4.2 Finding the places

- The **fast model** reads the page and returns a JSON list of up to 12 places it names:
  `[{"name": "Kenilworth Castle", "what": "castle in Warwickshire"}]`. Only real, mappable places
  (settlements, buildings, natural features, regions, battle sites); not people or ideas. Names
  are snapped to the page text where they appear (as highlights are), and deduplicated.
- For a **fork phrase**, the phrase itself is the one place (no model call).
- The fast model never gives coordinates.

### 4.3 Locating them

In order, stopping at the first hit, for each name:

1. **Cache** (`place_cache` table, keyed by the normalised name).
2. **Offline reference** (when the tier has `[reference]` and kiwix-serve answers): title
   suggestions in each book (exact title first, then the first suggestion whose title starts
   with the name), read the article's HTML, take the first `geo:LAT,LON` link (Wikipedia's
   coordinates template). The article title is kept.
3. **Online Wikipedia** (when the tier's `[web]` search isn't `off`):
   `https://en.wikipedia.org/w/api.php?action=query&prop=coordinates|description&redirects=1&titles=…`,
   then if that has no coordinates, one `list=search` call and the coordinates of its first hit.
   A descriptive `User-Agent` is sent, as Wikimedia asks.
4. Otherwise: not located.

Coordinates are validated (−90..90, −180..180, not 0,0). Lookups run a few at a time with a total
deadline of 30 s; whatever is located by then is shown, the rest listed as not located.

### 4.4 Storage and API

- `maps(id, node_id, n, phrase, status, error, created_at, updated_at)` and
  `map_places(map_id, idx, name, what, lat, lon, title, source)`; `source` is `reference`, `web`
  or `""` (not located). `place_cache(key, name, lat, lon, title, source, at)`.
- `POST /v1/maps {node_id, phrase?}` creates a map job and returns `{id}` (202); it runs in a
  small background worker (not the GPU queue). `GET /v1/maps/<id>` returns
  `{id, node_id, phrase, status: "finding"|"locating"|"done"|"failed", places: [...], error}`.
  `POST /v1/maps/<id>/again` re-runs it. `GET /v1/pages/<id>` lists the page's maps (like media).
- `/v1/status` adds `map_available: {ok, reason}`.
- `POST /v1/intent` gains `map` (typed "on a map" requests), returning the concept to open.

### 4.5 UI

- **Map window** (resizable, like the Image window): a status line while finding/locating, then
  the map fitted to the pins (a single pin at zoom 8), the place list beside or below it (by
  window width), **Again**, and the page's title. Pin labels show the place name.
- Pins use the kit's accent; the selected one the warm "in this context" colour.
- Clicking a pin or list item opens the place (General branch from the page). The concept opened
  is the place's article title when known, else the name.
- The Map button is disabled with the reason when the map is unavailable, as Image and Video are.
- The reader's thumbnail strip shows a map glyph for the page's saved maps; clicking reopens it.

## 5. Security

- Primer reads only the configured PMTiles file and assets folder; asset paths are resolved and
  must stay inside the folder; only `.pbf`, `.json`, `.png`.
- Wikipedia lookups go only to `https://en.wikipedia.org/w/api.php`, with the place name as the
  only user-derived data, and only when web search is enabled in the tier.
- `tools/map_extract.py` downloads only from the URL given (default: Protomaps' build server),
  writes only the output file, and checks the PMTiles header before writing.
- Place names and descriptions are rendered as text, never HTML.

## 6. Left to the builder

Prompt wording, the HTML coordinate parsing details, the exact window layout, marker styling,
and the extractor's command-line shape within the above.

## 7. This machine

World map `D:\Files\Reference\maps\world-z9-20261101.pmtiles` (1.6 GB, from Protomaps' 2026-11-01
build), assets in `D:\Files\Reference\maps\basemaps-assets` (commit 028c18f). Places come from the
Phase 4 ZIMs. The Protomaps `pmtiles` command-line tool couldn't be run here (downloaded
executables are blocked), which is why the extractor is Primer's own Python.
