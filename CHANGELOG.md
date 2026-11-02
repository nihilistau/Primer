# Changelog

## v0.2.0: images (2026-11-02)

Primer can now draw what you are reading.

### Added

- **Pictures from any page or phrase.** Use the Image button on a page or a branch fork, or type
  a request such as "draw a trebuchet at dawn". The fast model writes the image prompt from the
  page, and the picture renders in the background while you keep reading.
- **Image window.** Live step-by-step progress with a preview, then the picture with its prompt,
  model and settings. **Cancel**, **Again** (a new random seed), **Retry**, and **Edit & run** to
  change the prompt, model or settings. Click a word of the prompt to explore it as a page.
- **Thumbnails** of finished and failed pictures at the bottom of the page they belong to.
- **stable-diffusion.cpp `sd-server`**, started and stopped by Primer, with three model families:
  SDXL, Qwen-Image 2.1 and MOSS-Image.
- **Image models window.** Scan a folder to find complete models by reading their file headers,
  add and edit models by hand with settings per model, and choose the default.
- **Auto GPU sharing.** While a picture is drawn, Primer pauses the writing model on the GPU, then
  brings it back. A page asked for in the meantime waits its turn. Manual mode leaves your servers
  alone. LM Studio tiers unload their models for the picture (`pause = ["lms", "unload", "--all"]`).
- **Reproducible pictures.** Every picture records the seed it used.
- **Crash cleanup.** Model servers Primer launched end with it, and leftovers from a crash are
  stopped on the next start. A process is never stopped just because it holds a port.
- **Docs.** A how-to guide ([docs/USING.md](docs/USING.md)), screenshots, and an Images section in
  [docs/RUNNING.md](docs/RUNNING.md).

### Changed

- Local model servers on `127.0.0.1` / `localhost` now bypass any system HTTP proxy. Hosted tiers
  still use it.
- The first image model you add becomes the default. Deleting the default falls back to another.

### Fixed

- Refusals (403/415) now read the request body first, so Windows no longer resets the connection
  before the client sees the error.
- A tier that names the image engine kind (`sdserver`) for text is refused with a clear message.
- The image model list is written under a lock, and a corrupt `var/images.json` is never silently
  replaced with an empty list.

### Measured

On an RTX 2060 (12 GB), 1024x1024 and 20 steps:

| Model | Time |
| --- | --- |
| SDXL | about 40 s per image once loaded |
| Qwen-Image 2.1 Q4_K_M | about 4 minutes |
| MOSS-Image SFT | about 4.5 minutes including the load |

## v0.1.0: pages and branching

- Type any topic and Primer writes a page about it. A fast model marks the phrases worth following.
- Click a phrase to branch: in this context, general, or sideways, with titles guessed ahead.
- Back and forward, Deeper, Simpler and Regenerate (old versions are kept).
- Pages are cached in a concept graph. The map shows where you've been.
- Tiers (Light, Hybrid, Workstation, Hosted) for LM Studio, llama-server, the Kairos sp-daemon or a
  hosted API. Primer starts and stops the servers it manages.
- A desktop of movable, resizable windows in the browser, using the Kairos design language.
- Listens on `127.0.0.1` only and answers only its own page.
