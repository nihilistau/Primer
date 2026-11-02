# Primer Phase 3: Video — Design

Date: 2026-11-02 · Author: nihilistau (with Claude) · Status: approved by direction ("continue the next phases")
Builds on: the main design (`2026-11-01-illustrated-primer-design.md`, §2 lenses, §5 item 3) and Phase 2 (`2026-11-01-primer-phase2-images-design.md`), released as v0.2.0.

## 1. What this phase adds

Short clips, generated locally, from anywhere you're reading:

- the **Video** button in the branching fork (now enabled), for the clicked phrase;
- a **Video** button on a page, for the page as a whole;
- **Animate** in a finished Image window: the picture becomes the first frame of a clip;
- typing a request such as **"animate a trebuchet firing"**, "make a video of …", "film …", "show me a clip of …".

A request opens a **Video window** at once with live progress, while you keep reading. The finished clip plays in a loop in that window and sits as a thumbnail on the page it belongs to. Video is always a background job.

## 2. The user's decisions

- **Models:** the latest good open video models that run on this machine: Wan, LTX, Krea "and whatever else is the latest good", with few-step accelerators (4 or 8-step distilled models or LoRAs). **Uncensored** versions where they exist.
- **Files:** downloaded to `G:\MODELS\video` (the external archive) and **run from `D:\Files\Models\video`** (internal). Nothing assumes either path.
- **After each phase:** commit, push, cut the public export, update the documentation and screenshots.

## 3. Goals

- **Video families** join the image families as presets of the same model store. A family has a `kind` (`image` or `video`); the Image models window becomes **Models**, with an Images and a Videos section and a separate **default per kind**.
- **Runtime: the same managed `sd-server`** (stable-diffusion.cpp `master-1031`, already installed), using its native `POST /sdcpp/v1/vid_gen`. Primer asks for `output_format: "webm"` and stores the returned container as-is. One model is loaded at a time; switching between an image and a video model restarts sd-server, exactly as switching image models does today.
- **One GPU queue.** Image and video jobs share the ImageService worker and the GPU gate (auto mode pauses the writing model for either). A waiting page still takes priority between jobs.
- **Per-model video settings**: width, height, frames, fps, steps, CFG, sampler, flow shift, seed, avoid (negative), CPU offload, VAE tiling; for two-stage (MoE) Wan 2.2 A14B models also high-noise steps, high-noise CFG and the MoE boundary. Frames are aligned to the family's rule (Wan `4n+1`, LTX `8n+1`); sizes to the family's multiple.
- **LoRAs per model** (for few-step acceleration and style): a list of `{file, strength, stage}` where stage is `both`, `high` or `low` (Wan 2.2 A14B uses separate high- and low-noise LoRAs). All of one model's LoRAs must sit in one folder; Primer passes that folder as `--lora-model-dir` and each LoRA as a structured `lora[]` entry (sd-server ignores `<lora:…>` prompt tags). LoRAs are `.safetensors` only.
- **Image to video.** Families whose recipe supports a start image (Wan 2.2 TI2V 5B, Wan 2.2 I2V, LTX) can take a finished picture as `init_image`. Animate is offered only when the chosen video model can do it, otherwise it says why.
- **Writing the video prompt.** The fast model writes a prose prompt that describes the subject, the motion and the camera ("slow push-in…"), at most 80 words, from the phrase, the page title and an excerpt; for Animate it describes how the picture should move. If it fails, the plain request is used. Editable and re-runnable, as for images.
- **Typed requests**: a deterministic matcher like the image one ("animate X", "make/create/generate a video/clip/animation of X", "film X", "show me a video of X"); ordinary questions ("how do I make a video?", "animate vs inanimate") stay pages.
- **Video window**: progress (writing the prompt → waiting for the GPU → loading → step N of M → decoding → done), then the clip looping, muted, with controls; prompt (click words to explore), model, settings, seed, frames and duration; **Cancel, Again, Retry, Edit & run**. Failed and cancelled states as for images.
- **Storage**: `.webm` files in `var/media/` named by content hash; `media.kind = 'video'`; the page thumbnail strip shows video thumbnails (first frame, with a play badge) alongside pictures.
- **Scan finds video models**: GGUF `general.architecture` / safetensors key layout for Wan 2.1/2.2 (5B vs 14B, high vs low noise), UMT5-XXL, Wan 2.1 / 2.2 VAEs, LTX 2.3 transformer, Gemma text encoder, LTX connectors and VAEs, and LightX2V-style LoRAs; proposes complete models and lists what's missing.

## 4. Non-goals

- Hosted video (xAI etc.); audio for LTX clips (the audio VAE slot stays empty: silent clips); video-to-video, VACE, first-last-frame, sound-to-video; upscaling or frame interpolation; downloading models from inside Primer; stitching clips into longer films.

## 5. Security

Unchanged rules from Phase 2 apply to every new field: sd-server's path stays in the tier file; model and LoRA paths are absolute, local, not starting with `-`, no network/device paths, `.gguf`/`.safetensors` only (pickles refused); every request field comes from the validated settings schema; Primer decodes and writes the result itself under `var/media/`; `/v1/media/` serves `.webm` as `video/webm` with the same containment rules. The init image for Animate is read by Primer from its own `var/media/` by media id, never from a client-supplied path or upload.

## 6. Families (defaults from sd-server's recipes, adjusted for 12 GB and few-step use)

Recipes available in `master-1031`: `wan22-ti2v-5b`, `wan22-t2v`, `wan22-i2v`, `wan21-*`, `ltx23-dev`, `ltx23-distilled`, `ltx25-dev`, `hunyuan-video-15-*`. Primer ships five families (exact slots and defaults in the plan, Task 1): **Wan 2.2 TI2V 5B** (text or picture to video, light), **Wan 2.2 T2V A14B** and **Wan 2.2 I2V A14B** (two-stage, with LightX2V 4-step LoRAs), **LTX-2.3 distilled** (8 steps, CFG 1) and **LTX-2.3 dev**.

Downloaded for the author (to `G:\MODELS\video`, mirrored to `D:\Files\Models\video`, about 75 GB):

| Setup | Files |
|---|---|
| shared | `umt5-xxl-encoder-Q5_K_M.gguf` (city96), `Wan2.1_VAE.safetensors` |
| Wan 2.2 TI2V 5B | `Wan2.2-TI2V-5B-Q6_K.gguf`, `Wan2.2_VAE.safetensors`, Kijai's `Wan22_TI2V_5B_Turbo_lora_rank_64` |
| Wan 2.2 T2V A14B | QuantStack High/LowNoise Q4_K_M, lightx2v `wan2.2_t2v_A14b_{high,low}_noise_lora_rank64_lightx2v_4step_1217` |
| Wan 2.2 I2V A14B, uncensored | Bedovyy `dasiwaWAN22I2V14B_midnightflirt{High,Low}-Q4_K_M.gguf` (NSFW finetune), lightx2v i2v 4-step `1022` LoRAs, lopi999 `NSFW-22-{H,L}-e8` LoRAs |
| LTX-2.3 distilled | unsloth `ltx-2.3-22b-distilled-1.1-Q3_K_M.gguf`, distilled connectors and video VAE, mlabonne `gemma-3-12b-it-abliterated-v2.q4_k_m.gguf` (uncensored text encoder) |

Not used, with the reason: **Krea Realtime 14B** is a causal, autoregressive Wan 2.1 finetune with no GGUF and no stable-diffusion.cpp support; **LTX-2.5** needs a 26 GB text encoder converted by hand and is too heavy for 12 GB; **MiniMax-H3** needs a 32B text encoder. Few-step speed comes from the LightX2V LoRAs and LTX's distilled model instead.

## 7. Decisions made by Claude (marked for review)

- **Stay on sd-server master-1031**: it already has every video recipe needed; a newer build can be dropped in by changing the tier's `sd_server` path.
- **Video lives in the image subsystem** (same store file `var/images.json`, same service and queue) rather than a parallel copy: one GPU, one sd-server, one queue. The API keeps `/v1/images` and `/v1/image-models` and adds `kind` where needed; the UI calls the window Models.
- **Default clip**: about 2 to 3 seconds at a size the card handles (480p-class), because a 12 GB Turing card with 32 GB of RAM is slow at video; the user can raise frames and size per model.
- **Animate uses the finished image's file**, resized by sd-server to the video size; Primer doesn't crop.
- **Stall timeout** for video is the same setting; video steps are slow but each step reports progress, so the watchdog still works.

## 8. Left to the builder

- Header-sniffing rules for video files beyond §3's list.
- Video window layout and thumbnail styling (kit tokens only).
- Prompt wording.
- Tests use the fake sd-server, extended with `vid_gen` returning a tiny WebM.

## 9. Measured on the author's machine

(Filled in from live runs before handover: each family, seconds per step, total time per clip, VRAM, whether auto GPU mode paused and resumed the writing model.)

## 10. Verification expected before handover

- Unit and API tests with the fake sd-server.
- A live clip through Primer from each downloaded family, run from `D:\Files\Models\video`, recording times.
- Docs, screenshots, commit, push, public export and release v0.3.0.
