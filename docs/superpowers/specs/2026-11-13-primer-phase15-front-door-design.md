# Primer Phase 15: Kairos as the front door

Date: 2026-11-13. Follows the [roadmap](2026-11-04-primer-roadmap.md#phase-15-kairos-as-the-front-door)
and v0.13.0. Phase 14 (the driven browser) comes after this one, by choice. This release is
**v0.14.0**.

## 1. Intent

Kairos, the companion, can already see what the reader is doing in Primer. The activity rows
and the read-only `primer` MCP tool both exist. This phase lets Kairos **drive** Primer as well.
The reader says "show me Bodiam Castle" or "tell me a story about that" to Kairos, and Primer,
open in the browser, does it as if the reader had typed it there.

Primer's side only:
- a small, documented tool API on `127.0.0.1`;
- new actions on the existing MCP tool;
- the window that carries the commands out.

Kairos is never modified from this repo. The reader adds or keeps the MCP server in Kairos's own
config, as today.

## 2. Goals

1. **A switch, off by default.** Settings gets **Let Kairos drive Primer**, saved in
   `settings.json` as `remote_enabled`. While it is off:
   - every driving action answers 403 with "Kairos can't drive Primer: turn it on in Settings";
   - the read actions keep working as they do today.
2. **What Kairos can ask for** (the `primer` MCP tool's actions; the schema is unchanged):
   - `now`: what is open in Primer: the focused page (id, title), its open lenses, and whether a
     window is listening.
   - `trail {limit?}`: the most recent pages in trail order (id, title, the phrase it came from,
     when), 20 by default, at most 100.
   - `find {q}`: read pages whose title contains `q`, case-insensitive (id, title), at most 20.
   - `show {text}`: does what typing `text` into the focused Reader's ask box does: a page, a
     picture, a clip, a map, a timeline, a diagram or a story, routed by the same `/v1/intent`
     rules. "that", "it" or "this page" in the text means the focused page (see §2.4).
   - `open {id}`: opens a page already read, by id (from `trail`, `find` or an activity row).
   - `lens {kind, id?}`: opens a lens on a page, the focused page by default. `kind` is one of
     `picture`, `map`, `timeline`, `diagram`, `story` or `listen`, the same buttons as the page
     header. A lens that isn't available answers with the button's own reason ("Maps are not
     available right now.").
   - `help`, `status`, `page`, `reader`, `quiz_due` and `quest` stay as they are. `help` lists the
     new actions and says which ones need the switch.
3. **The window carries it out.**
   - A Primer browser tab listens for commands on an event stream.
   - When there are several tabs, the one focused most recently takes the command.
   - The command runs through the same code as the reader's own typing or clicking, so a
     Kairos-made page is an ordinary page on the trail.
   - A short note appears near the top of the desktop, above every window, "Kairos asked: show
     me Bodiam Castle", and fades after 8 seconds. Kairos-made pages carry `via: "kairos"` on their arrival edge, so the
     trail and the book can say how the reader got there.
4. **"That" means the focused page.**
   - Each tab reports its focused Reader's page to the server when the page changes and when the
     tab gains focus.
   - For `show`, the server replaces "that", "it", "this" or "this page", where it is the object
     ("a story about that", "a map of it", "show me that"), with the focused page's title before
     the command is sent. The tab also passes its focused page as the parent (`from_id`), as the
     ask box already does. With nothing focused, the answer is "Open a page in Primer first".
   - For `lens` without an id, the server uses the last reported focus.
5. **Answers Kairos can use.**
   - A driving action answers when the window has done it, or when 90 seconds pass:
     - "Opened *Bodiam Castle* (page 39dd…)";
     - "Started a map of *Bodiam Castle*; it will appear in Primer";
     - "Primer is writing *Bodiam Castle*; it's open in the Reader".
   - Long work (pictures, clips, stories) answers as soon as it starts, not when it finishes.
   - With no tab listening, the answer is "Primer isn't open in a browser. Open
     http://127.0.0.1:8810 first." Commands are not queued for later.
6. **The tool API is documented** in RUNNING.md, with the JSON shapes:
   - `GET /v1/tool/now`, `GET /v1/tool/trail?limit=`, `GET /v1/tool/find?q=`;
   - `POST /v1/tool/show {text}`, `POST /v1/tool/open {id}`, `POST /v1/tool/lens {kind, id?}`.
     Each answers 202 with `{command}`, and `GET /v1/tool/commands/<id>` gives its state:
     `queued`, `delivered`, `done` or `failed`, with `result` or `error`.
   - The window's side: `GET /v1/tool/stream` (the event stream),
     `POST /v1/tool/focus {node_id, title}` and `POST /v1/tool/commands/<id>/done {ok, result|error}`.

## 3. Safety

- **The switch.** Nothing drives Primer unless the reader turned the switch on in Primer's own
  Settings. Kairos can't turn it on: there is no API to set it except the Settings window's own
  route, which keeps the existing guard.
- **What a command can't do.** Commands can only do what the reader could do from the Reader and
  the page header. They can't:
  - delete anything, change settings, the tier or models, or run setup;
  - make a book;
  - open the browser lens on a URL;
  - answer quiz items or change notes.
- **The existing localhost guard applies to every route:** a foreign Host, Origin or Sec-Fetch-Site
  is refused, and every POST needs a JSON Content-Type. So a web page can't drive Primer.
- **Limits:**
  - `show` text is at most 300 characters;
  - ids are checked against the graph;
  - `kind` is checked against the list;
  - one command runs at a time (409 "Primer is still doing the last thing Kairos asked");
  - at most 20 commands a minute (429);
  - a command not picked up by a tab within 10 seconds fails with "Primer isn't open in a browser".
- **No queue survives a restart.** Commands live in memory only.
- **The note is visible.** The reader always sees what Kairos asked for.
- **Activity.** Kairos's row format (`harness/model/activity.py`: `at`, `event`, `title`, `ref`,
  `private`) has no origin field, so a page opened because Kairos asked is written with the
  event `kairos_opened_page` instead of `open_page`. Kairos then never reads its own command back
  as something the reader chose. The reader's own later actions on that page are written as
  usual.

## 4. Decisions (Claude's calls)

- **Drive the open window, not a hidden one.** Primer's work happens in the reader's view.
  Kairos asking for a page should look exactly like the reader asking, so the page lands on the
  trail, in the book and in the reader model. A headless path would need a second copy of the
  Reader's logic.
- **One stable MCP tool.** New actions arrive through `help`. Kairos's cached tool schema doesn't
  change, so no cold restart is needed beyond the one already paid to add the server.
- **Off by default, switched on in Primer.** Kairos driving the reader's screen is a step up from
  looking at it. The reader decides, in the app being driven.
- **Typed-text routing for `show`.** It reuses `/v1/intent` and the Reader's `routePrompt`, so
  "a map of Rome", "diagram of a siphon" and "a story about castles" all work with no new
  parser.
- **v0.14.0.** Releases stay in order even though phases don't.

## 5. Non-goals

- Changing Kairos, its prompt or its config (Primer's docs show the config to add).
- Speech, or Kairos reading Primer's narration aloud.
- Driving the browser lens or other websites (that's Phase 14).
- Remote access from other machines.

## 6. Left to the builder

- The note's styling, and where focus is reported in the UI.
- How the stream reconnects: back off from 1 s to 10 s, with a heartbeat comment every 15 s.
- **Tests must prove:**
  - the switch blocks every driving route;
  - a command reaches only the most recently focused listener, and fails after 10 s with none;
  - one-at-a-time and the rate limit;
  - every input is checked;
  - the MCP actions map to the routes and wait for `done` up to 90 s;
  - the UI runs `show`, `open` and `lens` through the existing typed and header paths, and marks
    pages `via: "kairos"`.
