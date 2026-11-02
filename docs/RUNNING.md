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

## Tests

```
pytest -q
cd ui && npx vitest run
```
