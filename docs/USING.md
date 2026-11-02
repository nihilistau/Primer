# Using Primer

A guide to reading, branching and drawing with Primer. To install it and point it at your models,
see the [README](../README.md#install). Every configuration option is in [RUNNING.md](RUNNING.md).

## The desktop

Primer opens as a small desktop in your browser. Windows can be moved by their title bar, resized
from any edge or corner, maximised (green), minimised to the dock (amber) or closed (red). The
dock at the bottom opens:

| Dock button | Window |
| --- | --- |
| Book | **New page**: a reader. You can open several readers side by side. |
| Grid | **Map of where I've been**: every page you've opened, as a tree. |
| Chip | **Engines**: the tier, the model servers and their states, and the GPU mode. |
| Wrench | **Image models**: add, scan, edit and choose image models. |

## Reading

Type anything into **Ask about anything...** at the bottom of a reader and press Enter: a topic
("castles"), a question ("why did castles stop being built?"), or a name ("Gilgamesh"). The page
streams in as it is written. When it is done, a small fast model marks the phrases worth
following, shown with a dotted underline.

The header of each page shows:

- **The source mode**, such as `storyteller`, which says what the page is grounded in. Storyteller
  pages come from the model's own knowledge. Offline-reference and web grounding are planned.
- **The version**, such as `v1`. Regenerating a page adds a version rather than replacing it.

The buttons beside it rewrite the page you are on:

| Button | What it does |
| --- | --- |
| **Deeper** | The same topic, written for someone who wants more detail. |
| **Simpler** | The same topic, written more plainly. |
| **Regenerate** | A fresh take on the page. |
| **Image** | Draws a picture of the whole page (see [Pictures](#pictures)). |

**Back** and **forward** (the arrows at the left) move through the pages you've visited in this
reader, like a browser. Going back shows the saved page instantly. It is never rewritten.

Pages are kept between runs. Typing a topic you've read before opens the saved page straight away.

## Branching

Click any underlined phrase. Primer fans out three branch cards, each with a title it has already
guessed for you:

- **In this context**: what the phrase means here, on this page. For example, "counterweight" on a
  trebuchet page explains the counterweight of a trebuchet.
- **General**: the phrase as a topic of its own.
- **Sideways**: a related idea worth a detour.

Click a card to open that page. Under the cards:

- **Image** draws just this phrase.
- **Ask about it** puts `Tell me about "<phrase>"` into the prompt box, so you can edit the
  question before sending it.
- **Video** is shown but not available yet; it arrives in a later version.

Press **Escape** or click elsewhere to close the fork without choosing.

## The map

**Map of where I've been** lists every page you have opened, indented under the page you branched
from. An icon shows how you got there: started there, typed, in context, general or sideways. Click any
row to open that page in a new reader.

## Pictures

There are three ways to ask for a picture:

1. **The Image button** in a page's header draws the whole page.
2. **Image on a branch fork** draws a single phrase.
3. **Typing a request** in the prompt box, such as:
   - "draw a trebuchet at dawn"
   - "paint / render / sketch / illustrate a castle keep at night"
   - "generate / create / make an image (or picture) of a siege tower"
   - "show me what a battering ram looks like"

   Ordinary questions that merely contain these words ("how do I draw blood?", "paint a rosy
   picture") are read as questions, not drawing requests.

An **Image window** opens at once and shows what is happening:

| Status | Meaning |
| --- | --- |
| Writing the prompt... | The fast model is turning the page or phrase into an image prompt. |
| Waiting for the GPU | Another image is ahead of this one, or the GPU is being freed. |
| Loading the model... | `sd-server` is starting and loading the model. The first image after a while takes longest. |
| Step N of M | Drawing, with a live preview that sharpens as it goes. |
| Done | The picture, its prompt, the model and the settings used. |
| Failed: ... / Cancelled | With the reason. A failed picture has a **Retry** button. |

You can keep reading, branching and opening pages while it draws. When it is done, the picture also
appears as a thumbnail at the bottom of the page it belongs to; click a thumbnail to open it again.

In the Image window:

- **Cancel** stops a picture that is still being made. The GPU is freed within about 15 seconds.
- **Again** draws it again with a new random seed.
- **Edit & run** lets you change the prompt, the model or the settings, then draw.
- Click any word of the prompt to explore it as a page.

Every picture records the seed it actually used, so you can reproduce one by running it again with
that seed.

## Image models

Open **Image models** from the dock.

- **Scan a folder**: paste the absolute path of a folder holding image models and click **Scan**.
  Primer reads the file headers (it never loads the models) and proposes each complete model it
  recognises, with its family and files. Click **Add** on the ones you want. A model that is
  missing a file is shown with what is missing.
- **New model**: choose a family and fill in the file paths yourself.
- **Click a model** to edit its name, files and settings. Each file shows `found` or `missing`.
- The **star** marks the default model, used for new pictures. The first model you add becomes
  the default; choose another with **Make default**.

Settings per model:

| Setting | Meaning |
| --- | --- |
| Width, Height | Picture size in pixels (256 to 2048; rounded to what the model needs). |
| Steps | More is slower and finer. 20 is a good start. |
| CFG | How closely to follow the prompt. SDXL likes about 5 to 7; Qwen-Image and MOSS-Image about 3 to 4. |
| Sampler | The denoising method. `euler` works for all three families. |
| Seed | Leave blank (or -1) for a new picture each time; set a number to repeat one. |
| Avoid | Things to keep out of the picture (a negative prompt). |
| Offload to CPU | Keeps parts of the model in system RAM. Slower, but lets big models fit a 12 GB card. |

The three supported families:

| Family | Files | Notes |
| --- | --- | --- |
| SDXL | One checkpoint (`.safetensors`), optional separate VAE | Fastest: about 40 s per image on an RTX 2060. |
| Qwen-Image 2.1 | Diffusion model, Qwen3-VL-8B text encoder, Qwen-Image 2.1 VAE | Follows long prompts well. About 4 minutes per image on an RTX 2060. |
| MOSS-Image | Diffusion model, Qwen3.5-9B text encoder, VAE, `tokenizer.json` | About 4.5 minutes per image on an RTX 2060. |

Keep model files on an internal drive. Loading from an external hard disk can take many minutes.

## Engines and tiers

**Engines** shows the tier you are using and lets you switch to another. Each model server has a
state:

| State | Meaning |
| --- | --- |
| ready | Primer started it, and it is answering. |
| external | It was already running (or you run it yourself). Primer uses it but never stops it. |
| starting | Primer started it and is waiting for it to answer. |
| stopped | Not running. The reason says why. |
| failed | It could not be started. The reason says exactly what went wrong. |

**Start** and **Stop** control a server by hand. The **Image server** row shows `sd-server`, which
Primer starts when a picture is needed.

### Sharing one GPU

With the GPU mode on **auto** (the default), when a picture needs the GPU, Primer pauses the writing
model, draws, then shuts `sd-server` down and brings the writing model back (about 20 seconds). A
page you ask for while a picture is drawing shows "paused for an image" and is written straight
after. On LM Studio tiers, pausing unloads LM Studio's models, and the first page afterwards waits
while LM Studio loads the model again.

With **manual** GPU mode, Primer leaves your servers alone and keeps `sd-server` loaded between
pictures. Use it if you have enough VRAM for both, or prefer to manage memory yourself.

## Tips

- **A good pair of models**: a 4B to 12B instruct model on the GPU for writing, plus a 1B to 2B
  model on the CPU as the fast model for highlights, prompts and branch titles.
- **Faster pictures**: SDXL at 20 steps is the quickest. Lower the steps for drafts.
- **Out of VRAM?** Turn on Offload to CPU, lower the size, or use auto GPU mode so the writing
  model steps aside.
- **Your data** lives in `var/`: the concept graph (`primer.sqlite`), pictures (`var/media/`) and
  your image models (`images.json`). Back up that folder to keep your book.

## Troubleshooting

| You see | Try |
| --- | --- |
| A server `failed` with "launch command not found" | The program in the tier's `launch` line isn't on your PATH. Use its full path. |
| `failed` with HTTP 401 | LM Studio has "Require API key" on. Put the key in a file and set `api_key_file` in the tier. |
| Pages never arrive | Check the Engines window, and that `model` in the tier matches what your server reports (`lms ls`). |
| Image button disabled | Hover it for the reason: no `sd_server` in the tier, no image model added, or no default chosen. |
| A picture failed with "Primer restarted" | Primer was stopped while it was drawing. Click **Again**. |
| "already answering" for the image server | Something else holds the image port, perhaps a leftover from a crash. Primer clears its own leftovers on the next start; otherwise stop it, or change `port` under `[images]`. |
| Thumbnails show a red cross | That picture failed. Open it to see why. |
