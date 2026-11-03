# Primer Phase 6: narrated stories — Design

Date: 2026-11-03 · Builds on: [the main design](2026-11-01-illustrated-primer-design.md) §5 (lens
5, "a narrated story: text-to-speech reading of a story-form page with accompanying images") and
§4.1 ("voice: selectable as local or hosted") · Status: Claude's design, written to the user's
standing instruction to continue the phases.

## 1. Intent

The Primer of the novel reads to its owner. Phase 6 gives Primer a voice. **Listen** reads the
page aloud. **Story** turns the page into a short illustrated story, told scene by scene: a
picture for each scene, the words highlighted as they are read, and every phrase still clickable.

Success: on the Warwick Castle page, **Story** opens a Story window. Within a minute the first
scene's narration is playing over its picture, while the rest is still being made. Clicking a
phrase in the scene text opens that page. Pulling the network cable changes nothing.

## 2. Goals

- **Listen** (reader header): the page read aloud, in a small Story window with no pictures.
- **Story** (reader header and branch fork): the writing model retells the page as a story of 3–5
  short scenes, each with a title. Each scene gets a picture (through the existing image queue,
  when pictures are available) and narration.
- **Typed**: "tell me a story about X", "read me a story about X", "a story about X" open the
  page for X and its story.
- **Story window**: the current scene's picture large, its text below with the sentence being
  read highlighted, **Play/Pause**, previous/next scene, and a scene strip. Playback starts with
  scene 1 as soon as its narration exists; later scenes join as they finish. Phrases in the text
  are clickable as on pages (the fast model's highlights for the story text).
- **Saved**: stories are kept with the page (text, pictures, audio) and listed in its thumbnail
  strip; reopening plays instantly. **Again** writes a new story.
- **Voice engine choice**: the tier's `[voice]` table. Two kinds:
  - `lfm3` — a llama-server running LFM3-Audio-2B with its mmproj, `POST /tts` → WAV (local, CPU,
    measured about 1.3× real time on the author's machine). Managed by Primer like the other
    servers, or external.
  - `openai` — any server with OpenAI's `POST /v1/audio/speech` (hosted, or a local Kokoro/Piper
    wrapper), `response_format: "wav"`.
  The voice (`female`, `male`, or the provider's names) is a setting.
- **Speech-friendly text**: Markdown stripped, citations `[n]` dropped, and numbers, years,
  ordinals and centuries spelled out ("1068" → "ten sixty-eight", "12th century" → "twelfth
  century"), because the model misreads digits (measured: "1068" was spoken as "760 date").
- **Unavailable with the reason**: no `[voice]`; the voice server isn't running. A story without
  pictures (no image model) is still a story; its scenes show their title instead of a picture.

## 3. Non-goals

- No speech input (asking by voice). No voice cloning. No music or sound effects.
- No streaming audio while a sentence is synthesised; a scene's audio is made, then played.
- No MP3/Opus encoding (no third-party Python): WAV, about 48 KB per second.
- No change to how pictures are made; a story's pictures are ordinary image jobs.

## 4. Decisions (Claude's calls)

### 4.1 Voice engine

```toml
[voice]
kind = "lfm3"
base_url = "http://127.0.0.1:8840"
voice = "female"
managed = true
launch = ["llama-server", "-m", "D:/Models/LFM3-Audio-2B-Q8_0.gguf",
          "--mmproj", "D:/Models/mmproj-LFM3-Audio-2B-F16.gguf", "--port", "8840", "-ngl", "0"]
```

A third server role, `voice`, managed like `text`/`fast` (`ManagedServer`), never GPU-gated
(CPU), health `GET /health`. `kind = "openai"` adds `model` and `api_key_file` like text engines.

Synthesis goes one request per chunk of at most about 300 characters, split at sentence ends,
one chunk at a time; a scene's chunks are joined into one WAV (same rate/width/channels checked;
a short silence between chunks). Each chunk's character range is kept so the window can highlight
the sentence being read (chunk-level timing, from the WAV frame counts).

### 4.2 Stories

- The writing model gets the page (title + Markdown) and writes, in storyteller voice, 3–5 scenes
  of 60–120 words, each starting with a `## ` title, total about 400 words; a child-friendly
  "illustrated primer" register; facts kept from the page.
- Scenes are parsed from the `## ` headings (tolerant: a story with none becomes one scene).
- Pictures: for each scene, `ImageService.create(node_id, phrase=scene title, subject=scene
  text)`, queued in order, when pictures are available. They render on the GPU through the usual
  queue; narration runs on the CPU at the same time.
- Narration is made scene by scene in order, so scene 1 can play while the rest is made.
- Highlights: the fast model's `highlight_messages` over the whole story text, as for pages.
- **Listen** is a one-scene story whose text is the page itself (no rewrite, no pictures).

### 4.3 Storage and API

- `stories(id, node_id, n, kind 'story'|'listen', title, status, error, voice, created_at,
  updated_at)`; `story_scenes(story_id, idx, title, text, media_id, audio, chunks_json,
  status)` — `audio` is `<sha256>.wav` in `var/media/`, `chunks_json` the `[start, end,
  seconds]` list. Story statuses: `writing`, `narrating`, `done`, `failed`; scene statuses
  `waiting`, `narrating`, `done`, `failed`. Unfinished stories are failed on restart.
- `POST /v1/stories {node_id, kind}` → 202 `{id}`; `GET /v1/stories/<id>` → story with scenes,
  highlights and each scene's picture (media id, status, file); `POST /v1/stories/<id>/again`;
  `GET /v1/pages/<id>` adds `stories`. `/v1/media/` also serves `<sha256>.wav` (`audio/wav`,
  ranges as for video). `/v1/status` adds `voice_available`; the servers list includes `voice`.
- `POST /v1/intent` adds `story: <concept|null>` (and then image/video/map are null).

### 4.4 UI

- **Story window**: picture (or the scene title on the kit's surface when there is none), text
  with the current chunk highlighted, controls, scene strip, status line ("Writing the story…",
  "Narrating scene 2 of 4…"), **Again**. Audio is an `<audio>` element per scene, advancing to
  the next scene when one ends (if it is ready; else it waits and says so).
- Reader: **Listen** and **Story** buttons (disabled with the reason); typed story requests;
  stories in the thumbnail strip with a story glyph. Fork: **Story** action.
- Engines: the **Voice server** row, and the voice name.

## 5. Security

- The voice server is launched only from the tier, as an argument list; `base_url` for `lfm3`
  must be on this machine (127.0.0.1/localhost/::1).
- Audio files are written only into `var/media/` by hash, served by the existing media route.
- Story text is model output rendered as text (with the existing Markdown renderer, which already
  escapes HTML).

## 6. Left to the builder

Prompt wording, the number-to-words details (within the cases above), chunk sizes, the window's
layout, and test structure.

## 7. This machine

LFM3-Audio-2B Q8_0 and its F16 mmproj copied from `G:\MODELS\LFM3` to `D:\Files\Models\LiquidAI`
(checksums match). Measured with llama-server build 12019 on the CPU: 8.6 s of speech in 11.4 s,
5.4 s in 7.0 s (RTF about 1.3), 24 kHz mono. A round trip through the same model's speech
recognition returned the text exactly, except "1068" heard as "760 date", hence the number
spelling.
