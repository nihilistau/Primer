# Running Primer

This is the setup and configuration reference. For how to use Primer, see [USING.md](USING.md).

Phase 1 is "pages and branching": open a concept, click phrases to branch, go back and forward,
browse the concept graph. Phase 2 adds images (see [Images](#images)) and Phase 3 video (see [Video](#video)). Everything runs on your machine; the server binds to `127.0.0.1` only.
The API answers only Primer's own page: the `Host` must be `127.0.0.1:<port>` or
`localhost:<port>`, a POST must be JSON, an `Origin` on any API request must be Primer itself, a browser's
`Sec-Fetch-Site` must be `same-origin` or `none`, and `/v1/branches` needs the `X-Primer: 1`
header the UI sends (403/415 otherwise; the Vite dev proxy rewrites Host, and Origin only for
its own page, for `npm run dev`).

## Prerequisites

- Python 3.11+. Runtime has no third-party dependencies; `pytest` is needed only for tests.
- Node 20+ (to build the UI).
- A text model server. Any one of:
  - **LM Studio** (`lms` on your PATH), serving an OpenAI-compatible API on port 1234.
  - **llama-server** from llama.cpp, with a GGUF model.
  - **sp-daemon** (Shannon-Prime). The backend (`kind = "sp"`) is implemented, but it was **not
    verified against a live daemon in Phase 1**. Treat it as untested.
  - or a hosted OpenAI-compatible API (the `hosted` tier).

## Build and run

```
cd ui && npm install && npm run build
cd ..
python serve.py
```

Open http://127.0.0.1:8810. Options:

| Option | Meaning |
| --- | --- |
| `--tier NAME` | Tier to use: a file name in the tiers directory, without `.toml`. Default: the one you last chose (a saved choice that is no longer valid is ignored with a warning), else one suggested for your hardware (by VRAM: 20 GB+ `workstation`, 8 GB+ `hybrid`, otherwise `light`). |
| `--port N` | Port (default 8810). |
| `--tiers-dir DIR` | Read tier files from another directory instead of `tiers/`. |

Runtime state (the concept graph, settings, keys) lives in `var/`, which is git-ignored. Switching
tier in the UI is remembered in `var/` and used the next time you start without `--tier`.
Pages are cached in the graph: reopening a concept after a restart is near-instant (about 0.1 s
in testing), and a fresh page takes as long as your model needs.

## Setup

The **Setup** window (see [USING.md](USING.md#setup)) downloads a starter kit and writes a tier
for it. The kits are listed in `primer/setup/kits.json`: each item names its source (a Hugging
Face repository and file, a Kiwix book and flavour, a GitHub release asset pattern, or a map
zoom), and the exact files, sizes and hashes are looked up from the live catalogues when you
check the plan.

Where things go, under the folder you choose:

| Path | What |
| --- | --- |
| `models/<publisher>/<repo>/<file>` | Model files. |
| `reference/<file>.zim` | Wikipedia. |
| `maps/<file>.pmtiles`, `maps/basemaps-assets/` | The map and its fonts and icons. |
| `tools/<name>/<release>/` | llama.cpp, stable-diffusion.cpp and kiwix-tools, unpacked. |
| `.setup/` | Downloads in progress (`.part` files) and unpacking. |

Each unpacked folder gets a `.primer-setup.json` marker recording what was installed there.
Setup's own state (the plan, progress, licences accepted, approvals) is `var/setup.json`. The
tier is written to `tiers/local-<kit>.toml` (git-ignored), with free ports chosen from 8191
(text), 8192 (fast), 8820 (pictures), 8830 (reference) and 8840 (voice). Model servers that
run on the CPU from a GPU build of llama.cpp get `--device none`, so they never take GPU memory
from pictures.

Safety:

- **Where downloads come from.** The first request must be to huggingface.co,
  download.kiwix.org, library.kiwix.org, github.com, api.github.com,
  objects.githubusercontent.com, codeload.github.com or build.protomaps.com. Redirects (mirrors
  and CDNs) may go to any `https` address that isn't on your own network; never `http`.
  `tools/map_extract.py` follows the same rules on every request and redirect (its default
  source must be build.protomaps.com; one given with `--source` must be public `https`), and
  ignores proxy settings.
- **Hashes.** A file is moved into place only after it matches its published SHA-256 (MD5 for
  kiwix-tools, which publishes nothing else). The map fonts are pinned to a commit and a hash in
  `kits.json`; the map is cut by `tools/map_extract.py` and read back to check it.
- **Formats.** Pickle formats (`.ckpt`, `.pt`, `.pth`, `.bin`, `.pkl`) are refused. Zips are
  unpacked into an empty folder first, refusing absolute paths, `..`, links (the map fonts'
  alias links are skipped), Windows device names and anything that expands to more than four times its size.
- **Programs** run only after you approve them in Setup. The approval is the file's SHA-256: a
  changed file, or an unapproved program or DLL beside it, is refused ("approve … in Setup before
  it can run"), and the server shows as unavailable until you do. Programs you configured
  yourself, outside a Setup folder, are not affected.
- **The Hugging Face token** (`var/keys/huggingface.key`) is sent only to huggingface.co.
- **Disk space.** A kit doesn't start unless it fits with a 2 GB margin; a download that fills
  the disk stops and keeps its `.part` to resume.

The routes are under `/v1/setup` (`GET` for the overview, `plan`, `start`, `pause`, `resume`,
`cancel`, `licence`, `approve`, `approve-release`, `updates`, `models`, `move`, `move/cancel`),
with the same origin rules as the rest of the API; `/v1/setup/updates` also needs `X-Primer: 1`.
`/v1/status` reports `setup: {needed, running}`.

## Tiers

A tier is a TOML file in `tiers/` naming a `[text]` engine for pages and a `[fast]` engine for
small jobs such as picking highlight phrases (`fast = "text"` reuses the text engine). The
shipped tiers are `light`, `hybrid`, `workstation` and `hosted`. Their model names are
**placeholders**: edit them to what you have installed.

```toml
name = "mine"

[text]
kind = "openai"                 # "openai" (LM Studio, llama-server, hosted) or "sp"
base_url = "http://127.0.0.1:1234"
model = "your-model-id"         # must match the server's model id exactly
managed = true                  # true: Primer launches and stops it; false: you run it
launch = ["lms", "server", "start"]
stop = ["lms", "server", "stop"]    # needed when the launcher exits once the server is up
pause = ["lms", "unload", "--all"]  # optional; frees the GPU for an image without stopping it
gpu = true                      # it uses the GPU: auto GPU mode pauses it for images
ready_timeout_s = 120           # how long to wait for it to become healthy
api_key_file = "var/keys/key.txt"   # optional; a file holding the token (relative to the repo root)

[fast]
kind = "openai"
base_url = "http://127.0.0.1:8081"
model = "your-small-model"
managed = true
launch = ["llama-server", "-m", "models/small.gguf", "--port", "8081", "-ngl", "0"]
```

Notes:

- **Model names** must match what the server reports. For LM Studio, list them with `lms ls`.
- **llama-server** is launched by Primer and stopped when Primer exits. A GPU model for text
  plus a small CPU model (`-ngl 0`) as the fast engine works well.
- **LM Studio**: `lms server start` is a launcher that exits once the server is up. Primer
  handles this, and uses `stop = ["lms", "server", "stop"]` to shut it down.
- **"Require API key"** in LM Studio makes every request fail with HTTP 401. Put the token in a
  file (keep it under `var/`, which is git-ignored) and set `api_key_file` on the tier (a
  relative path is relative to the repo root, not the current directory). The
  Engines window then reports "requires an API key" or "rejected the API key" if it is missing
  or wrong.
- **Extra request fields.** A `[text.extra]` (or `[fast.extra]`) table is added to every chat
  request to that engine, for options your server understands. Thinking models such as Gemma 4
  can spend a whole page's token budget thinking, especially on grounded pages with long
  sources, and then write nothing ("empty response"). With llama-server, turn thinking off:

  ```toml
  [text.extra]
  chat_template_kwargs = { enable_thinking = false }
  ```

  `messages`, `max_tokens`, `temperature`, `stream` and `model` can't be overridden this way.
  A hosted API may reject fields it doesn't know.
- Small fast models often paraphrase highlight phrases instead of copying them. Primer snaps
  each phrase to the exact text on the page, so clickable phrases still land.

## Server states (Engines window)

Each engine shows one of five states, plus a `reason` when there is something to say:

| State | Meaning |
| --- | --- |
| `external` | Already running when Primer started (or `managed = false` and healthy). Primer uses it but never stops it. |
| `stopped` | Not running. The reason says why, e.g. "not managed and not running" (start it yourself), or a note from a stop: "process had exited" (it was already gone), "launched via a launcher; no stop command configured" (the server it started may still be running; add a `stop` command), or a failed stop command. |
| `starting` | Primer launched it and is waiting for it to answer health checks, up to `ready_timeout_s`. |
| `ready` | Primer launched it and it is healthy. Primer stops it on exit. |
| `failed` | It could not be started. The reason is specific: launch command not found, process exited with a code, not healthy within the timeout, or an HTTP problem such as 401 (API key needed or rejected). |

Anything a configuration cannot do is shown as unavailable with its reason rather than hidden.

If Primer itself is killed or crashes, the servers it launched directly (llama-server,
`sd-server`) do not keep running: on Windows they end with Primer (a job object), and on every
system Primer records them in `var/children.json` and, on its next start, stops any that are
still running the same program started at the same time ("stopped a leftover ... server from the
last run"). Primer never stops a process just because it holds a port, and what a launcher such
as `lms server start` starts (the LM Studio app) is left alone.

## Verified so far

Checked by hand on one machine (RTX 2060, 12 GB): llama-server for text with a
gemma-4-E4B-it Q6_K model on the GPU and a LiquidAI LFM2.5-1.2B-Instruct Q8_0 on the CPU as the
fast model, both launched and stopped by Primer. A page of about 300 words took about 85 s. LM
Studio's launcher behaviour and API-key handling were exercised as described above. The sp-daemon
path has not been run against a real daemon.

## Images

Phase 2 draws pictures with [stable-diffusion.cpp](https://github.com/leejet/stable-diffusion.cpp).
Primer launches its `sd-server` when an image is needed and stops it afterwards; you never run it
yourself.

### Install stable-diffusion.cpp

Download a build from the project's official GitHub releases page and unpack it anywhere. Pick the
build for your hardware: CUDA (NVIDIA), Vulkan (other GPUs) or CPU only. Then point the tier at the
`sd-server` executable (uncomment the `[images]` example in the tier file):

```toml
[images]
sd_server = "C:/path/to/stable-diffusion.cpp/sd-server.exe"
port = 8820                # default
ready_timeout_s = 900      # how long to wait for the server to load a model (default 900)
stall_timeout_s = 1800     # a running job with no progress this long is marked failed (default 1800)
```

Without `sd_server`, image generation shows as unavailable with its reason. Measured with
stable-diffusion.cpp master-1031 (Windows CUDA 12 build).

### Add models (Models window)

Open the Models window and use **Scan folder** to find models in a folder, or **Add** to
register one by hand. Each model has its own settings (size, steps, cfg, sampler, seed and so on,
with defaults per family), and one model is the **default** used for new requests. Files must be
local: network and device paths are refused, and **pickle files (`.ckpt`, `.pt`, `.pth`, `.bin`)
are refused**; use `.safetensors` or `.gguf`.

| Family | Files needed |
| --- | --- |
| SDXL | One checkpoint file (an optional separate VAE). |
| Qwen-Image 2.1 | The diffusion model, the Qwen3-VL-8B text/vision model, and Qwen-Image 2.1's own VAE. |
| MOSS-Image | The diffusion model, the Qwen3.5-9B text model, a VAE, and the tokenizer (`tokenizer.json`). |

Keep models on an internal drive. Loading SDXL from an external HDD took about 7 minutes; from an
internal SSD it takes seconds.

### Asking for an image

Click a phrase and choose the Image button, or type a request such as "draw a trebuchet at dawn".
Recognised forms: "draw / paint / render / sketch / illustrate a ...", "generate / create / make an
image / picture of ...", and "show me what a ... looks like". The image appears as a thumbnail on
the page; **Again** re-runs it with a new random seed (each image records the seed it used), and
**Edit & run** lets you change the prompt, model or settings first. If Primer stops mid-job, the job
is shown as failed ("Primer restarted") after the next start.

### GPU mode

With `gpu_mode = "auto"` (the default) and a tier whose text server uses the GPU, Primer stops the
writing model for the image, then stops `sd-server` and brings the writing model back. Measured:
the writing model (gemma-4-E4B) went from 5.1 GB of VRAM to 1.0 GB while drawing and was ready again
about 20 s after the image finished. A page requested during an image or video waits ("paused for a
picture or video") and is written right after. With `gpu_mode = "manual"`, Primer leaves your servers alone and
`sd-server` stays loaded; tiers with no GPU text servers also keep `sd-server` loaded.

A text server with a `pause` command is paused by running that command instead of being stopped.
LM Studio needs this: `lms server stop` only stops its HTTP server, and the loaded model would keep
its VRAM. The shipped `hybrid` and `workstation` tiers therefore set
`pause = ["lms", "unload", "--all"]`, which unloads every model LM Studio has loaded (also when
LM Studio was already running before Primer started) and leaves its server up. The model loads
again on the next page request, so keep LM Studio's Just-in-Time model loading on (its default);
the first page after an image waits for that load.

### Measured speeds

On one machine (RTX 2060, 12 GB), 1024x1024, 20 steps. Your hardware will differ.

| Model | Speed |
| --- | --- |
| SDXL checkpoint | about 1.3 s/step, about 40 s per image once the model is loaded |
| MOSS-Image SFT (UD-Q4_K_XL, Qwen3.5-9B Q8_0 encoder, CPU offload) | about 5.4 s/step with the GPU to itself (about 17 s/step when other apps held about 5.5 GB of VRAM); first image about 4.5 min including the load |
| Qwen-Image 2.1 Q4_K_M with Qwen3-VL-8B INT8 (CPU offload) | about 11 s/step through Primer with the writing model paused; 234 s for one image (about 23 s/step when other apps held about 5.5 GB of VRAM) |

## Video

Phase 3 makes short clips with the same `sd-server` (its native `vid_gen` API, WebM output). No
extra setup is needed beyond the `[images]` block: add a video model in the Models window and it
becomes the default video model. Pictures and clips share one queue, one GPU and the same auto
GPU mode.

### Video models

| Family | Files | Where to get them (examples) |
| --- | --- | --- |
| Wan 2.2 TI2V 5B | diffusion GGUF, UMT5-XXL encoder, Wan 2.2 VAE | `QuantStack/Wan2.2-TI2V-5B-GGUF` (model and `VAE/Wan2.2_VAE.safetensors`), `city96/umt5-xxl-encoder-gguf` |
| Wan 2.2 T2V A14B | high-noise and low-noise GGUFs, UMT5-XXL, Wan 2.1 VAE, optional LoRAs | `QuantStack/Wan2.2-T2V-A14B-GGUF`, 4-step LoRAs from `lightx2v/Wan2.2-Distill-Loras` |
| Wan 2.2 I2V A14B | as T2V, image-to-video models | `QuantStack/Wan2.2-I2V-A14B-GGUF` (or a finetune), `lightx2v/Wan2.2-Distill-Loras` i2v LoRAs |
| LTX-2.3 distilled / dev | diffusion GGUF, Gemma 3 12B GGUF, embeddings connectors, video VAE | `unsloth/LTX-2.3-GGUF`, a Gemma 3 12B instruct GGUF |

A layout the folder scan understands, with encoders shared between models:

```
video/
  shared/umt5-xxl-encoder-Q5_K_M.gguf
  shared/VAE/Wan2.1_VAE.safetensors
  wan22-ti2v-5b/Wan2.2-TI2V-5B-Q6_K.gguf, VAE/Wan2.2_VAE.safetensors
  wan22-t2v-a14b/HighNoise/…, LowNoise/…, loras/…
  ltx23-distilled/distilled-1.1/…, text_encoders/…, vae/…, gemma-3-12b-….gguf
```

LoRAs must be `.safetensors`, all of one model's LoRAs in one folder (Primer starts `sd-server`
with `--lora-model-dir` set to it). When a scanned model has LightX2V or other few-step LoRAs,
its proposal comes with matching settings (for Wan 2.2 A14B: 4 steps, 2 of them high-noise, CFG 1).

Animate sends the finished picture to `sd-server` as the clip's first frame. Primer reads it from
its own `var/media/`; no path or upload is accepted from the browser.

### Measured video speeds

On one machine (RTX 2060 12 GB, 32 GB RAM), models and `sd-server` on an internal SSD, CPU offload
and VAE tiling on.

| Model | Clip | Time |
| --- | --- | --- |
| Wan 2.2 TI2V 5B Q6_K, 20 steps, CFG 5 | 832x480, 49 frames (2 s at 24 fps) | about 6 min: 10 s/step, 2 min 15 s to decode |
| Wan 2.2 T2V A14B Q4_K_M with the LightX2V 4-step LoRAs: 2 high-noise + 4 low-noise steps, CFG 1 | 832x480, 33 frames (2 s at 16 fps) | about 6.5 min: 47 s/step (both 14B halves are swapped through RAM), 51 s to decode; noticeably sharper than the 5B |

Kijai's Wan 2.2 5B Turbo LoRA at 8 steps and CFG 1 took half the time but ignored the prompt, so
it isn't recommended.

## Grounding

Phase 4 lets a page be written from sources: an offline copy of Wikipedia (**offline
reference**) or the live web (**web**), as well as the model's own telling (**storyteller**).
The default for new pages is the tier's `source_mode`, until you choose another in the Engines
window (remembered in `var/settings.json`). Any page can be rewritten in another mode from its
badge.

### Offline reference (Kiwix)

1. Download [kiwix-tools](https://download.kiwix.org/release/kiwix-tools/) for your system
   (on Windows, `kiwix-tools_win-x86_64-3.8.1.zip`) and unpack it anywhere.
2. Download one or more Wikipedia ZIM files from
   [download.kiwix.org/zim/wikipedia](https://download.kiwix.org/zim/wikipedia/). Check each
   against its `.sha256`. Good choices for English:

   | File | Size | Contents |
   | --- | --- | --- |
   | `wikipedia_en_top_nopic_<date>.zim` | 2.6 GB | the 50,000 most-read articles, in full, no pictures |
   | `wikipedia_en_all_mini_<date>.zim` | 13 GB | every article's opening section |
   | `wikipedia_en_all_nopic_<date>.zim` | 49 GB | every article in full, no pictures |

   Keep them on an internal drive, like the models.
3. Add `[reference]` to your tier:

   ```toml
   [reference]
   kiwix_serve = "D:/Tools/kiwix-tools/kiwix-serve.exe"
   zims = ["D:/Reference/wikipedia_en_top_nopic_2026-09.zim",
           "D:/Reference/wikipedia_en_all_mini_2026-09.zim"]
   port = 8830
   ```

   Primer starts `kiwix-serve` on `127.0.0.1` when it starts, shows it in the Engines window as
   the **Reference server**, and stops it on exit. It uses no GPU. To use a `kiwix-serve` you run
   yourself, give `base_url = "http://127.0.0.1:8830"` instead of `kiwix_serve` and `zims`.

When the same article is in more than one ZIM, the one listed first wins, so list the full
articles before the "mini" ones. Primer looks for an article whose title matches the page first,
then runs a full-text search, and reads up to three articles (about 6,000 characters in all).
Source links open the article in `kiwix-serve`'s own page, in a new browser tab.

### Web

Web mode needs no setup: it searches DuckDuckGo and reads the top pages. To choose another
search engine, or turn web mode off, add `[web]` to the tier:

```toml
[web]
search = "duckduckgo"      # or "searxng", "brave" or "off"
# searxng_url = "http://127.0.0.1:8888"     # for searxng
# api_key_file = "var/keys/brave.key"       # for brave
```

Primer fetches up to five results at once and uses the first three that work, waiting at most
30 seconds. Only `http`/`https` pages on public addresses are fetched (never `localhost` or
your local network), at most 2 MB each.

### When there's no source

If the encyclopedia has nothing on the topic, `kiwix-serve` isn't running, or no web page could
be read, the page is written from the model's own knowledge, recorded as storyteller, and the
reader shows why.

### Measured grounding speeds

On the same machine, writing with gemma-4-E4B (Q6_K, thinking off) on llama-server:

| Mode | Finding sources | Whole page |
| --- | --- | --- |
| Offline reference (`wikipedia_en_top_nopic`) | 0.3 s | about 9 s |
| Web (DuckDuckGo, three pages) | about 6.5 s | about 15 s |

## Maps

Phase 5 adds the map lens: the places a page names, pinned on an offline map. It needs two things
on disk, both named in the tier's `[map]` table:

```toml
[map]
pmtiles = "D:/Reference/maps/world-z9.pmtiles"
assets = "D:/Reference/maps/basemaps-assets"
```

### The map file

A [PMTiles](https://docs.protomaps.com/pmtiles/) vector map from
[Protomaps](https://protomaps.com/), OpenStreetMap data. Primer reads it itself and serves its
tiles; nothing is fetched from a tile server. `tools/map_extract.py` cuts one out of Protomaps'
public daily build (139 GB, of which it downloads only what you ask for):

```bash
python tools/map_extract.py --out D:/Reference/maps/world-z9.pmtiles --maxzoom 9 --dry-run
python tools/map_extract.py --out D:/Reference/maps/world-z9.pmtiles --maxzoom 9
```

| Max zoom | Size | Detail |
| --- | --- | --- |
| 8 | about 560 MB | regions and large towns |
| 9 | about 1.6 GB | towns, main roads (recommended) |
| 10 | about 3.8 GB | small towns, more roads |

To add more detail for one region, give a box and a deeper zoom for it, for example England and
Wales to zoom 12:

```bash
python tools/map_extract.py --out D:/Reference/maps/world-z9-uk-z12.pmtiles --maxzoom 9 --bbox -6.5,49.8,2.0,55.9 --region-maxzoom 12
```

The tool resumes if interrupted, and needs about two to three times the finished file's size
free while it runs. `--dry-run` prints the size first.

### Fonts and icons

Labels and map icons come from Protomaps'
[basemaps-assets](https://github.com/protomaps/basemaps-assets) (the fonts are under the SIL Open
Font License). Download the repository as a zip, unpack it, and point `assets` at the folder
holding `fonts/` and `sprites/`.

### How places are found

The fast model reads the page and lists up to 12 places it names, each with what kind of place
it is. Each is then located from a Wikipedia article's coordinates, in this order:

1. Places already located before (kept in `var/primer.sqlite`).
2. The offline encyclopedia (`[reference]`), trying a qualified title first ("River Avon
   (Warwickshire)" for "a river in Warwickshire").
3. When `[web]` search isn't `off`: Wikipedia's API, then Wikidata's coordinates for articles
   without any (counties and regions often have none). Only the place's name is sent.

A place with no coordinates anywhere is listed as not found. The model's own idea of where a
place is is never used. Lookups stop after 30 seconds and show what was found.

With LFM3-1.7B as the fast model (on the CPU) and the two Wikipedia ZIMs, a page's map takes
about 20 seconds; a single place, about 9 seconds including the page.

## Voice

Phase 6 gives Primer a voice, for **Listen** and **Story**. It needs a text-to-speech server,
named in the tier's `[voice]` table. Two kinds work:

- **`lfm3`**: llama.cpp's `llama-server` running Liquid AI's **LFM3-Audio-2B** with its
  multimodal projector. It answers `POST /tts` with a 24 kHz WAV. It runs on the CPU, so it never
  competes with pictures for the GPU.
- **`openai`**: any server with OpenAI's `POST /v1/audio/speech` (a hosted API, or a local
  Kokoro or Piper wrapper). Add `model`, and `api_key_file` if it needs a key.

```toml
[voice]
kind = "lfm3"
base_url = "http://127.0.0.1:8840"
voice = "female"                   # or "male"
managed = true
launch = ["llama-server", "-m", "D:/Models/LFM3-Audio-2B-Q8_0.gguf",
          "--mmproj", "D:/Models/mmproj-LFM3-Audio-2B-F16.gguf",
          "--port", "8840", "-ngl", "0", "-c", "8192"]
ready_timeout_s = 180
timeout_s = 180                    # seconds allowed per spoken passage
```

Primer starts the voice server with the others and shows it in the Engines window as the
**Voice server**. Use a recent llama.cpp; the `/tts` route came with LFM3-Audio support. For an
`lfm3` server, `base_url` must be on this machine. `timeout_s` (default 180) is how long one
passage may take to voice; raise it on a busy CPU. `ready_timeout_s` is how long Primer waits for
the server to start, and `stop` (a command list, as for the other servers) shuts down a detached one.

Text is prepared for speaking first: Markdown and citations are dropped, and numbers, years,
ordinals, centuries, money and common units are spelled out. Measured on this machine, the
model said "768" for "1068" written as digits, and "ten sixty-eight" correctly. Narration is
made in passages of up to 300 characters and joined, with a quarter-second pause between them.

### Measured voice speeds

On an RTX 2060 machine's CPU (8 threads), LFM3-Audio-2B Q8_0 made 8.6 seconds of speech in 11.4
seconds and 5.4 seconds in 7.0 (about 1.3 times real time) when the machine was otherwise idle.
Through Primer, with the writing model on the GPU:

| | Speech | Ready |
| --- | --- | --- |
| **Listen**, the Warwick Castle page (three parts) | 85 s | first part after 16 s, all after 98 s |
| **Story**, four scenes | 95 s | written and fully narrated after 138 s; scene 1 plays after about 70 s |
It slows down a great deal when other programs keep the CPU busy: with three other model servers
using most of the CPU, one sentence took over four minutes. Give it a free CPU, or put it on the
GPU (`-ngl 99`) if you have about 3 GB of video memory to spare. Primer doesn't pause a GPU
voice server while pictures are made, so that only suits a card with room for both.

## Browser

Phase 7's Browser needs no setup of its own. It reads web pages through the same guarded fetcher
as web grounding (so `[web] search = "off"` also turns off web browsing), and offline articles
through `kiwix-serve` (`[reference]`). Primer turns each page into its own clean Markdown; a
page's HTML, scripts and styles are never served from Primer's address. Pictures go through
`/v1/browse/image`, which only passes PNG, JPEG, GIF and WebP, checked from their bytes.

Measured with the offline encyclopedia: an article opens in about 0.1 s, a Wikipedia web page in
about 1 s; Ask answers in about 3 s and a two-step click-through finishes in about 5 s with
gemma-4-E4B writing.

## Thinking ahead

Phase 8 drafts the likely next page on the idle GPU (spec:
`docs/superpowers/specs/2026-11-04-primer-phase8-drafting-design.md`). Drafting takes a
background turn at the GPU gate: only when nothing holds or waits for it and the text model isn't
paused for pictures, and it is pre-empted within about a second when anything else asks. That
bound holds for local (loopback) servers, where the stream can be cancelled even before the
server answers; a remote engine can't be stopped before it starts answering, so a page asked for
then may wait longer. Opening another page drops the old page's drafts (the new page offers its
own); a draft stopped for a picture is tried again once the GPU is idle.

The setting is `thinking_ahead` (0, 1 or 2) in `var/settings.json`, set from the Engines window.
Unset, it is 1 when `gpu_mode = "auto"`, the `[text]` engine has `gpu = true` and its `base_url`
is on this machine (127.0.0.1, localhost or ::1), and 0 otherwise: hosted (paid API) and CPU-only
tiers are off unless you turn it on. Lowering it (two pages to one) stops a draft two pages ahead.

Measured with gemma-4-E4B: a draft was ready about 20 seconds after its parent page finished;
opening the drafted card took 0.00 s instead of about 8 s; a new page asked for in the middle of a
draft waited 0.25 s for the draft to step aside, and its first text came at 1.9 s (about 1.6 s
without a draft).

## Tests

```
pytest -q
cd ui && npx vitest run
```
