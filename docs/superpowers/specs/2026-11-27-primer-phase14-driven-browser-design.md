# Primer Phase 14: the driven browser

Date: 2026-11-27. Follows v0.15.0 and the [roadmap](2026-11-04-primer-roadmap.md#phase-14-the-driven-browser).
Status: **draft for the user's review**. Ships as **v0.16.0**.

## 1. Intent

Phase 7 built a **reader** browser. It rebuilds a page as Primer's own clean text, so the page's
own scripts never run and it can't log in. Many pages need scripts, though, or a sign-in: a
museum's opening-hours widget, a timetable, a library catalogue, a site you have an account on.
Phase 7 agreed on "reader now, Chrome later". This phase is the later.

**Live** mode opens such a page in a real browser that Primer starts and controls. The browser
runs out of sight, with no window of its own. Primer shows it inside a Browser window as a live
picture that you can click, scroll and type into. The writing model can also take a goal, read
the live page, and click through it for you, the way Click through does in the reader today. Any
step that goes beyond reading and following links waits for your OK.

The reader stays the default. Live is for pages that need it.

## 2. What the reader sees

1. **Ways in:**
   - **Live** in the Browser window's bar opens the current address live.
   - A reader page that comes out nearly empty, or that says it needs JavaScript, offers "This
     page needs its scripts. **Open it live**".
   - An address typed into a live window stays live.
2. **The live view:**
   - The page is drawn as a picture that refreshes as it changes, at the window's size.
   - Clicking, scrolling and typing go to the page.
   - Back, Forward, Reload and the address bar work as in the reader.
   - A small "Live" badge sits in the bar, with **Reader** to go back to the reader view of the
     same address.
3. **Signing in.** You sign in yourself, typing into the live view as on any site. Primer's
   browser has its own profile, separate from your everyday browser, and it remembers sign-ins
   between sessions. The Browser window's menu has **Forget everything (sign-ins, cookies,
   history)**, which asks first.
4. **Click through, live.**
   - The same side panel as in the reader takes a goal ("Find this weekend's opening hours").
   - Each step shows what the model saw, what it chose and why.
   - It may **scroll, read, and follow links** on its own, for up to 8 steps in 180 s.
   - **Anything else stops and asks**, every time: pressing a button, typing into a field,
     choosing from a list, or submitting a form. The panel says "The model wants to press **Add
     to basket**" or "The model wants to type **'Bodiam'** into **Search**", with **Allow once**
     and **Stop**.
   - **It never types into a password field**, and never reads one.
   - **Stop** ends it at any time.
5. **Ask and Make a page** work on a live page too. They use the live page's text, as the reader
   uses the rebuilt text.
6. **What's not live:**
   - Downloads are refused, with the note "Primer's browser doesn't download files".
   - Pop-ups open in the same view.
   - Camera, microphone, location and notifications are always refused.

## 3. How it is built (Claude's calls unless marked as the user's)

- **The browser (the user's choice: whichever is found).**
  - Primer looks for Chrome, then Edge, in their standard install places. A `[browser]`
    `path = "…"` setting in the tier overrides that.
  - The Setup centre shows what was found: "Live pages: Chrome 158", or "Live pages need Chrome
    or Edge".
  - Nothing is downloaded.
- **Starting it:**
  - headless (`--headless=new`), with Primer's own profile directory, `var/browser/`;
  - remote debugging on `127.0.0.1` at a port Chrome chooses (`--remote-debugging-port=0`, read
    back from `DevToolsActivePort`);
  - no `--remote-allow-origins`, so web pages can't reach the control port;
  - started on first use, and closed after 10 minutes with no live window open, and when Primer
    stops.
  - One browser serves up to 4 live windows, one tab each.
- **Talking to it.** The DevTools Protocol goes over a WebSocket. Primer needs a small WebSocket
  client written in the standard library, since there are **no new dependencies**.
- **The picture:**
  - `Page.startScreencast` sends JPEG frames at quality 70 and at most 10 per second, sized to the
    window;
  - the server sends them on to the window over the existing SSE pattern;
  - input travels back as small JSON posts (mouse, wheel, key, text), checked and turned into
    `Input.dispatch*` calls.
- **The model's view (the user's choice: text and numbered controls).**
  - A script reads the live page into the same shape the reader's Click through uses: title,
    text excerpt, and a numbered list of **controls**.
  - Each control has its kind (link, button, field, list), its visible label, and for links the
    address.
  - Password fields are listed as "password field" with no value.
  - Text models work as they do today. Screenshots to a vision model are out of scope.
- **What the model may do (the user's choice):**
  - On its own: `scroll {up|down}`, `follow {link n}` (only links listed, and only http(s)), and
    `answer {text}`.
  - With your OK each time: `press {control n}`, `type {field n, text}` (text at most 200
    characters, never into a password field) and `choose {list n, option}`.
  - Replies are JSON, checked like today's agent.
  - Page text is **data, not instructions**. The prompt says so, and the model can only act
    through the listed controls. It can never type an address.
- **Keeping local services safe:**
  - Every request the live browser makes is checked through the DevTools `Fetch` domain.
  - Requests to **loopback, private, link-local, and other non-public addresses** are refused.
    Requests whose host resolves to one of those are refused too, checked at request time. So are
    schemes other than http(s), data: and blob:.
  - This keeps a page from reaching Primer, Kairos, LM Studio or your router.
  - `file:`, `chrome:` and `edge:` addresses can't be opened.
  - Downloads are denied (`Browser.setDownloadBehavior`), and permission requests are denied.
- **Limits:**
  - 4 live windows;
  - an input post is at most 4 KB, and typed text at most 1000 characters a post;
  - a frame is at most 2 MB;
  - an idle live window stops its screencast when hidden.
- **Routes** follow the localhost guard: POSTs are same-origin JSON. They are:
  - `POST /v1/live/open {window, url}`;
  - `GET /v1/live/<window>/frames` (SSE);
  - `POST /v1/live/<window>/input`;
  - `POST /v1/live/<window>/nav {back|forward|reload|url}`;
  - `POST /v1/live/<window>/close`;
  - `POST /v1/live/<window>/agent {goal}` (SSE of steps), and `POST
    /v1/live/<window>/agent/answer {allow|stop}`;
  - `POST /v1/live/forget` (wipes `var/browser/`);
  - `GET /v1/live/status`.
- **Settings.** `[web] search = "off"` turns live mode off too, as it does web browsing today.

## 4. Safety summary

- **Separate profile.** The live browser never uses your everyday browser profile, passwords or
  cookies.
- **No reaching local services.** Pages can't reach local or private network addresses. The
  control port takes connections from this computer only, and refuses web pages.
- **The model asks first.** It can't press, type, choose or submit without your OK each time. It
  never touches password fields, and never types an address.
- **No downloads**, and no camera, microphone, location or notifications.
- **Nothing runs in Primer.** Only pictures of the page reach Primer's window. The page's HTML and
  scripts never run there.

## 5. Non-goals

- Screenshots for a vision model (possible later, when a vision model is loaded).
- Kairos driving the live browser (Phase 15's tools stay as they are).
- More than one tab per window, bookmarks, extensions, devtools, printing and file uploads.
- Saving passwords for you. You type them each time a site asks, or the site remembers you
  through its own cookies.
- Accessibility of the live picture itself. The page is pixels, so screen readers get the
  reader view and the agent's text steps instead. This is said plainly in the docs.

## 6. The user's answers (2026-11-27)

1. **What the model may do without asking:** scroll, read and follow links. Anything that types,
   presses, chooses or submits asks first, every time.
2. **Sign-ins:** remembered in Primer's own profile, with Forget everything.
3. **The model's view:** page text and numbered controls; no screenshots.
4. **The browser:** whichever is found, Chrome first, then Edge, with a setting to choose.

## 7. Tests the plan should require

- **The WebSocket client:** framing, masking, fragmented and large messages, close and ping.
  Tested against a stdlib test server.
- **Address checks:** loopback, the 172.16/12 and 169.254 ranges, IPv6 local, and names that
  resolve to them are all refused. Public addresses pass.
- **The agent:**
  - `follow` only accepts listed http(s) links;
  - `press`, `type` and `choose` always wait for an answer;
  - `type` into a password field is refused outright;
  - bad JSON gets one retry;
  - Stop ends it;
  - the step and time limits hold.
- **Input:** posts over 4 KB, unknown kinds and bad coordinates are refused. Key and text events
  map to the right DevTools calls.
- **Lifecycle:** a fake DevTools endpoint covers start, idle shutdown, 4-window limit and
  Forget.
- **The UI:** frames draw, input is sent at the right scale, the Allow once / Stop prompt, and
  "Open it live".
- **Live run:** with installed Chrome, check a script-only page, a sign-in by hand, Click through
  with one Allow once, and a page trying to fetch `127.0.0.1` being refused.
