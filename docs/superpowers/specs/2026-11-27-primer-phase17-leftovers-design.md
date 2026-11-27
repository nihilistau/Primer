# Primer Phase 17: the leftovers

Date: 2026-11-27. Follows v0.16.0 and the [roadmap](2026-11-04-primer-roadmap.md#phase-17-the-leftovers).
Status: **draft for the user's review**. Ships as **v0.17.0**.

## 1. Intent

Clear every small item parked in Phases 11 to 16, as the user asked on 2026-11-27, so nothing is
left scattered. The one large item is closing the live browser's DNS-rebinding gap with a guarding
proxy. The user chose to build it, which also turns safe public WebSockets back on for live
pages.

Each item below is one line in the roadmap. Here it gets the behaviour a reader should see once it
is fixed.

## 2. The guarding proxy (the user's choice: build it)

**Today.** Primer checks each live-page request's host through the DevTools `Fetch` domain, and
then Chrome connects by resolving the name itself. A host that answers a public address to the
check and a local one to Chrome gets through. That gap is narrowed by Chrome's local-network
block, but not closed. WebSockets are switched off entirely, because Fetch can't see them.

**After:**
- **One way out.** Primer runs a small proxy on `127.0.0.1` at a port the system picks. Primer's
  browser sends **all** its traffic through it:
  - `--proxy-server=http://127.0.0.1:<port>`;
  - `--proxy-bypass-list=<-loopback>`, so even loopback names go through the proxy;
  - `--disable-quic`, so nothing goes over UDP around it.
- **What the proxy does with each connection:**
  - It handles `CONNECT host:port`, for https and wss, and absolute-form http requests, for http
    and ws.
  - It resolves the host **once**. If every address is public (the same rules as
    `primer.grounding.web._check_host`, including the local and single-label name rules), it
    connects to **that checked address**. Otherwise it answers 403.
  - Chrome never resolves names for proxied traffic, so a rebinding answer can't reach Chrome.
  - Ports are limited to 80, 443 and 8080–8443. Others get 403.
- **Limits:**
  - 256 open connections; then 503.
  - Connecting times out after 10 s, the name lookup after 3 s, and an idle tunnel after 120 s.
  - A request head over 16 KB gets 400.
  - The proxy stops with the browser.
- **The Fetch check stays.** It still refuses local addresses with a clear page error, and it
  stays a first layer. The proxy is the layer that enforces.
- **WebSockets.** `WebSocket` and `WebSocketStream` work again in live pages, through the proxy.
  WebTransport and WebRTC stay blocked, because they use UDP and would bypass the proxy.
- **The docs** say the gap is closed. The changelog says live chat widgets and tickers work.
- **Who can use it.** The proxy listens on loopback only. Any other program on this computer that
  used it could still only reach public addresses, which it already can. No credentials are
  needed (Claude's call; Chrome can't send proxy credentials for WebSocket tunnels from a flag).

## 3. The other leftovers, as the reader should find them

### The bound book (Phase 16)
- **The announcement:** when the page count settles, the live region announces the exact count
  once ("Pages 2–3 of 40").
- **The toolbar:** the arrow keys move between its buttons when focus is in it (a roving
  tabindex), and the page-turn keys work only on the book.
- **Component tests** cover key turning, Home and End, reduced motion, the link check before
  insert, and the HTML check before insert.
- **Re-layout:** a key pressed while a long book is laid out again is queued, and applied from
  where the reader lands. It is never undone.
- **Double-click** in a page's outer third selects a word when the click isn't a turn: a second
  click within 400 ms of a turn is treated as selection.
- **After a lost pointer capture,** no click is swallowed. Only a real drag suppresses the click.
- **"Open in the Browser?"** for an http(s) source link is checked live with a real web
  citation.

### Your book (Phase 13)
- **Maps:** a chapter shows only the **latest** finished map for each phrase, so Warwick Castle
  gets one plate, not five.
- **A failed status read** during a book job leaves the book's files alone and marks the book
  failed, with a reason.
- **Code blocks:** a line indented 4 or more spaces is code, not a heading.

### Timelines and diagrams (Phase 12)
- **The 1st millennium CE** gets its own readable bucket, at century resolution, so its events
  can be seen.
- **The final date list** is the last list the model gave, unless its labels are a strict subset
  of an earlier, fuller list. Then the fuller one wins.
- **Regnal numbers:** the rule no longer accepts the pronoun "I" ("Unclear I think"). "Henry I"
  still works.
- **Window mount tests** cover the Timeline and Diagram windows: Again, close, and clicking a
  label or an event.
- **SVG `use` and markers** are kept in diagrams, but only when they point inside the same SVG
  (`href="#id"`). External or data: references are dropped.

### The continuing story (Phase 11)
- **Chapter 1's look-back** skips pages already used by another tale that day.
- **Picture prompts:** a near-repeat of the look (the same words in another order, or a light
  rewording) is removed, not only an exact repeat.
- **A crash** between a chapter's story and its row can't leave an orphan. The story and its row
  are written together, and a startup sweep removes any orphans from older versions.

### The driven browser (Phase 14)
- **Service workers:** one whose set-up fails is closed before it runs. If that can't be
  guaranteed, service workers are blocked in live pages; workers that need them fall back to the
  network.
- **Worker set-up** has its own pool and can't hold a DevTools thread for 9 s. Stuck lookups are
  cancelled and free their pool slots.
- **Resize** takes the window's lock.
- **The orphan sweep** closes a window under its lock, and only if it is still unwatched.
- **A Make-a-page retry** after its live window has closed falls back to the page's address.
- **Ask's live address** is cleaned by `parse_address`, the same as Make a page's.
- **An oversize post** is refused without waiting for its body.
- **Test nits:** the out-of-order WebSocket test waits on a signal, not a sleep, and the test
  server's errors are asserted.
- **A changed `[browser] path`** applies on a tier switch, with no restart.
- **Pool threads** don't hold up Primer's stop: daemon workers, with a bounded join.
- **The image-service test** of a server dying mid-job waits on state, not time, so it is steady
  under load.
- **Visibility check:** the press's check also refuses a control hidden by `filter: opacity(0)`,
  a `clip-path` or `mask` that hides it, or a near-zero `transform: scale`.

## 4. Non-goals

- Anything new beyond these items.
- Proxy credentials, an upstream or corporate proxy, and HTTP/3.

## 5. Tests the plan should require

- **The proxy:**
  - CONNECT to a public test host is tunnelled, and to 127.0.0.1, `[::1]`, `172.31.0.5`,
    `169.254.169.254`, `localhost` and a single-label name gets 403;
  - a name whose second lookup would differ is connected to its **first, checked** address (an
    injected resolver);
  - the port rule, the connection cap, the head cap, timeouts and stopping;
  - a real-Chrome smoke test where Chrome's traffic goes through the proxy, and a page's
    `new WebSocket` to `ws://127.0.0.1:<port>` fails;
  - in the live run, a public site that uses WebSockets works.
- **Each other item** gets a test that fails before its fix.
