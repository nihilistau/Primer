# Primer Phase 2: Images — Design

Date: 2026-11-01 · Author: nihilistau (with Claude) · Status: draft for approval
Builds on: `docs/superpowers/specs/2026-11-01-illustrated-primer-design.md` (§2 lenses, §4 engines and GPU modes, §5 item 2) and the finished Phase 1 (`phase-1` branch).

## 1. What this phase adds

Pictures, generated locally, from anywhere you're reading:

- the **Image** button in the branching fork (now enabled), for the clicked phrase;
- an **Image** button on a page, for the page as a whole;
- typing a request like **"generate an image of a trebuchet at dawn"** (or "draw…", "make a picture of…", "show me an image of…") in any prompt box.

Each request opens an **image window** at once with a placeholder and live progress, while you keep reading. The finished image is saved against that page in the concept graph, and the page shows a small strip of its images so the book fills up with pictures.

Image models are **selectable, each with its own settings**, managed in a new **Image models** window.

## 2. The user's decisions

- **Engine: stable-diffusion.cpp**, which runs all of the user's image models (verified this session: SDXL renders through both `sd-cli` and `sd-server`).
- **Per-model settings and model selection**, with a default model and per-image override.
- **Typed requests work**: "generate an image of X" and similar.
- **The user's models**, in `G:\MODELS\image` on the author's machine:
  - **SDXL**: `bigLove_xl4.safetensors`, `bigLove_lust1.safetensors` (single-file SDXL checkpoints).
  - **Qwen-Image 2.1**: diffusion `qwen-image-2.1-UC-Q8_0.gguf` / `qwen-image-2.1-Q4_K_M.gguf` (also an NVFP4 safetensors), text encoder `qwen3vl_8b_int8_convrot.safetensors`, VAE `qwen_image_2.1_vae_bf16.safetensors`.
  - **MOSS-Image SFT**: diffusion `Moss-Image/MOSS-Image-SFT-UD-Q4_K_XL.gguf`, plus the files downloaded this session into the same folder: text encoder `Qwen3.5-9B-Q8_0.gguf`, VAE `vae/diffusion_pytorch_model.safetensors`, tokenizer `tokenizer/tokenizer.json` (per unsloth's model card).
  - **LTX**: only an LTX-2 19B LoRA is present. Video is Phase 3 and will need a base model; nothing for it here.
- stable-diffusion.cpp `master-1031` (Windows CUDA 12 build, includes `sd-cli.exe` and `sd-server.exe`) is installed at `G:\Tools\stable-diffusion.cpp\master-1031`.
- **G: is an external HDD.** The user will normally run models from C: or D: once space is freed, so every path is configurable and nothing assumes G:.

## 3. Goals

- **Model families and models.** Primer ships *family* presets — `sdxl`, `qwen-image-2.1`, `moss-image` — each saying which files it needs (diffusion model or single checkpoint, text encoder, VAE, tokenizer) and its default settings. A *model* is a family plus the user's file paths plus its settings. Models live in the user's own settings (`var/`), never in the repo, because their paths are machine-specific.
- **Scan a folder.** In the Image models window, "Scan folder…" reads a folder (e.g. `G:\MODELS\image`), identifies files by their headers (GGUF `general.architecture`; safetensors metadata/key layout: SDXL `conditioner.embedders…`, `qwen_image_2.1_vae`, etc.) and proposes ready models, plus incomplete ones that list what's missing ("MOSS-Image: needs a text encoder"). The user confirms. Nothing is downloaded automatically.
- **Settings per model**: steps, CFG scale, sampler, width × height, seed (random by default), negative prompt, CPU offload, flash attention, VAE tiling, flow shift (for flow-matching families). Each family's defaults come from upstream docs (§6). Validated ranges; unknown keys rejected.
- **Unavailable with the reason.** A model with missing or unreadable files, or sd-server not configured, shows as unavailable with why; the Image buttons say why instead of failing later.
- **Writing the image prompt.** The fast model turns the request into a prompt suited to the family: tag-style for SDXL, natural sentences for Qwen-Image and MOSS. Context: the phrase, the page title, a short page excerpt. If the fast model fails or returns junk, use the plain request text. The prompt shown in the image window is editable and re-runnable.
- **Typed requests.** A deterministic matcher recognises image requests in the prompt box ("generate/create/make/draw/paint/render/show me … an image/picture/illustration/drawing/painting of X", "picture of X", "draw X"). It doesn't create a page; it opens an image job for X attached to the page you're on, or to X's concept node when the reader is empty. Not a model call, so it's instant and predictable.
- **Runtime: a managed `sd-server`** (§7). Primer launches `sd-server` with the selected model's files as startup flags, as a managed server like the text engines (role `image`, `gpu = true`), and talks to its native async API: `POST /sdcpp/v1/img_gen` (prompt, negative_prompt, width, height, seed, `sample_params.sample_steps`, `sample_params.sample_method`, `sample_params.guidance.txt_cfg`, …; returns a job id), `GET /sdcpp/v1/jobs/{id}` (status `queued|generating|completed|failed|cancelled`, progress via `preview.step/total_steps` when previews are requested, result `images[].b64_json`), `POST /sdcpp/v1/jobs/{id}/cancel`, and `GET /sdcpp/v1/models/status` for load state. The model stays loaded between images. Switching models restarts `sd-server` with the new model's flags.
- **Family defaults come from sd-server's recipes** (`GET /sdcpp/v1/recipes`: `sdxl-checkpoint`, `qwen-image-21`, `qwen-image-21-turbo`, `moss-image-sft`, `moss-image-base`, … each with `slots` and `defaults`); Primer maps its families to recipe ids and layers the user's per-model settings on top.
- **Asynchronous jobs, one GPU job at a time.** Primer queues jobs and submits one at a time to sd-server. The image window shows writing prompt → queued → loading model → step N of M → done. Cancel uses sd-server's cancel endpoint.
- **GPU memory (auto mode, from the main spec).** With `gpu_mode = "auto"`, before an image job Primer waits for any page currently being written to finish, stops managed text/fast servers marked as using the GPU, runs the job, then restarts them. Pages requested meanwhile wait with a visible "paused for an image" note. With `gpu_mode = "manual"`, jobs just run. The Engines window shows when the writing model is paused for an image.
- **Storage.** Images are PNG files in `var/media/` named by content hash, recorded in a new `media` table: node, phrase, model, prompt, negative prompt, settings, seed, status, file, error, created time. Reopening a page shows its existing images; nothing regenerates unless asked ("Again" = same settings, new seed).
- **Image window.** Resizable, one per image; shows the image fitted, the prompt (editable), model and settings used, seed; buttons: Again, Edit prompt & run, Use another model, Open file location. Clicking a word in the prompt opens it in a reader (exploration never dead-ends).

## 4. Non-goals

- Image editing, img2img, inpainting, LoRAs, ControlNet (later).
- Video (Phase 3).
- Downloading models from inside Primer.
- sd-server's own model-folder scanning and runtime model switching (it needs ComfyUI-style category folders); Primer passes files as startup flags instead.

## 5. Security (builds on Phase 1's localhost-only API)

- The **sd-server executable path is not settable through the API.** It lives in the tier file (`[images] sd_server = "…"`, plus `port`), like model-server launch commands. Only local files can name an executable.
- Model file paths *are* set through the UI, but Primer only passes them to sd-server as model/encoder/VAE/tokenizer startup arguments, never as output paths or extra flags. sd-server binds 127.0.0.1 only. Allowed extensions: `.gguf`, `.safetensors`, `.json` (tokenizer). **`.ckpt`/`.pt` pickle files are refused** (they can execute code when loaded).
- No free-form extra sd-server arguments or request fields from the API: every flag and job field comes from the validated settings schema. Primer decodes the returned image and writes it itself inside `var/media/`.
- Image and media routes follow Phase 1's rules: POSTs JSON-only with Origin checks; GET routes with side effects (none planned) would need `X-Primer`; media files are served read-only from `var/media/` with the same path-containment rules as the static UI.
- "Scan folder" reads file headers only (fixed small byte counts), never loads or executes anything.

## 6. Family presets (defaults from stable-diffusion.cpp docs and model cards)

| Family | Files | Defaults |
|---|---|---|
| Family (sd-server recipe) | Startup flags | Defaults (recipe defaults, adjusted by docs) |
|---|---|---|
| `sdxl` (`sdxl-checkpoint`) | `-m` checkpoint | 1024×1024, CFG 5–7, sampler `euler_a`, 20 steps, VAE tiling on |
| `qwen-image-2.1` (`qwen-image-21`) | `--diffusion-model`, `--llm` (Qwen3-VL-8B), `--vae` (Qwen-Image 2.1 VAE only) | 1024×1024 (multiples of 32), 20 steps, sampler `euler`, CFG per recipe/docs, CPU offload on |
| `moss-image` (`moss-image-sft`) | `--diffusion-model`, `--llm` (Qwen3.5-9B), `--vae`, `--tokenizer` | 1024×1024 (multiples of 16), CFG 3, 20–50 steps, flow shift 5, CPU offload on |

Flags verified against `sd-server --help` of `master-1031`; the builder re-reads recipes from the running server.

## 7. Decisions made by Claude (marked for review)

- **Managed `sd-server` with startup flags** rather than per-job `sd-cli`: measured this session, a 6.5 GB SDXL load from the external HDD took ~7 minutes, while a job against an already-loaded sd-server took 42 s. Keeping the model resident is the difference. Switching models restarts the server; auto GPU mode stops it like the text servers.
- **Models stored in `var/images.json`**, edited through the UI; family presets ship in `primer/images/families.py` (code, not config).
- **Typed-request matching is a regex**, not a model call.
- **Default model on the author's machine**: SDXL `bigLove_xl4` (fastest on 12 GB); the user can change it.
- **GPU-using servers are marked** with a new optional `gpu = true` in a tier's engine spec; auto mode only pauses those.
- **One image job at a time**; a queue for the rest.
- **Images attach to the current page**, or to the requested concept's node when no page is open.
- **No new dependencies**: Primer decodes sd-server's base64 PNG with the standard library.
- **Job lifecycle: one create call.** `POST /v1/images` creates a job whose first stage is *writing the prompt* (fast model), then *queued*, *loading*, *step N of M*, *saving*, *done* / *failed* / *cancelled*. The window shows the prompt as soon as it exists. Editing the prompt (any time after it exists) and pressing Run creates a new job; it never mutates a running one.
- **Prompt before pause.** The prompt is written when the job is created, before any GPU pause, so the fast model is never needed while servers are stopped. On the shipped tiers the fast model runs on CPU and isn't marked `gpu`.
- **Scheduling in auto mode.** Consecutive image jobs keep the GPU servers down until the queue drains, *except* that a waiting page request takes priority: after the current image job finishes, servers restart, the page is written, then remaining image jobs continue. A running image job is never interrupted for a page.
- **Restart behaviour.** On startup, media rows left `queued`/running are marked `failed` with "Primer restarted"; serve.py already starts the tier's servers, so a crash mid-job can't leave them stopped.
- **Typed requests attach** to the reader window they were typed in: its current page, or — when that reader is empty — X's concept node, created like a typed page node but without generating a page. `media.node_id` references `nodes.id`. The Reader's prompt box is the only prompt box (the fork's "Ask about it" fills it).
- **Scanning with alternates:** one proposed model per diffusion variant (e.g. Qwen Q8_0, Q4_K_M, NVFP4 as three models), each paired with the family's encoder/VAE/tokenizer found in the same folder or its subfolders; when several candidates fit, pick by name similarity and let the user change it.
- **Shipped tiers get `gpu = true`** on their GPU text engines (hybrid and workstation text); light and hosted have none.
- **A page's image strip** shows every image attached to that page; phrase images are labelled with their phrase.

## 8. Left to the builder

- Job polling interval, timeouts, and how progress previews are requested from sd-server.
- Exact header-sniffing rules for scanning, beyond the examples above.
- Image window layout and thumbnail strip styling (kit tokens only).
- The prompt-writing wording per family.
- Tests use a fake sd-server (stdlib HTTP) implementing the four native endpoints and returning a tiny PNG.

## 9. Measured on the author's machine (RTX 2060 12 GB, ~5.5 GB VRAM already used by other apps, models on an external HDD)

| Model | Path | Result |
|---|---|---|
| SDXL bigLove XL4, 1024², 20 steps | `sd-cli` | Works. ~1.3 s/step; model load ~7 min from the HDD while it was busy |
| SDXL bigLove XL4, 1024², 20 steps | `sd-server` native API, model resident | Works. 42 s per image |
| Qwen-Image 2.1 Q4_K_M + Qwen3-VL-8B INT8 + 2.1 VAE, 1024², 20 steps, CPU offload | `sd-server` native API | Works. First image 17 min total: sampling 7.6 min (~23 s/step), decode 40 s, the rest loading. Resident-model images ≈ 8–9 min |
| MOSS-Image SFT UD-Q4_K_XL + Qwen3.5-9B Q8_0 + VAE + tokenizer, 1024², 20 steps, CFG 3, CPU offload | `sd-server` native API | Works. First image 9.2 min: sampling 5.6 min (~17 s/step), decode 8 s, ~6 min loading. Resident ≈ 6 min. In-image lettering is garbled at Q4/20 steps — more steps or a higher quant for legible labels |

Implications: keeping models resident (sd-server) is essential; Qwen-Image 2.1 on this card is a "start it and keep reading" model, so the image window's progress and the page thumbnail strip matter. A faster drive (C:/D:) will shorten loads, not sampling.

## 10. Verification expected before handover

- Unit and API tests with the fake sd-server.
- A live run on the author's machine of each ready family (SDXL, Qwen-Image 2.1, MOSS-Image) through Primer, recording time per image and whether auto GPU mode paused and resumed the writing model.
