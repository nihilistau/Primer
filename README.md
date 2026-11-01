# Primer

An offline, local-first **illustrated primer**. Start from a loose interest, say castles, and
follow your curiosity: every phrase on a page is clickable, branching into a page written in
context, a general article, or a sideways jump. Any concept can open in another lens, such as an
image, a short video, a map or a narrated story, each in its own resizable window on a
desktop-style workspace.

It is an extension of chatting with a language model into something you can navigate like a
website. You can go back and forward, follow a branch, or just type.

## Status

**Design stage.** Nothing runs yet. The design is in
[docs/superpowers/specs/](docs/superpowers/specs/). Build order:
pages and branching → images → video → grounding (offline Wikipedia / web) → maps → narrated stories → an
embedded browser the model can see and drive.

## Principles

- **Local first, everything a choice.** The writing model can be LM Studio, llama-server, the
  Kairos sp-daemon or a hosted API. Image, video and voice are each local or hosted. Suggested
  tiers (Light, Hybrid, Workstation, Hosted) fit different hardware, and you can adjust anything.
- **Never wait on a spinner.** Images and video are background jobs. You keep reading while they render.
- **Everything is kept.** Pages and media are cached in a concept graph, so your exploration
  becomes a book you can browse later.
- **Choose your grounding.** Storyteller, offline reference or web, labelled on every page.

Sibling project of [Kairos](https://github.com/nihilistau/Kairos), whose desktop UI and design
language Primer shares.

## License

MIT. See [LICENSE](LICENSE).
