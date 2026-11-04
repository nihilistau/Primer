# Primer Phase 9: the setup centre — Design

Date: 2026-11-04 · Builds on: [the roadmap](2026-11-04-primer-roadmap.md) Phase 9 and the main
design's goal "for other people with other hardware" · Status: Claude's design; the user chose
this phase next.

## 1. Intent

Today, getting Primer going means hunting down a dozen files by hand: model GGUFs, runtimes,
Wikipedia files, a map, fonts, then editing a tier file. The **Setup** window turns that into one
screen: pick a starter kit suited to this machine, see what it costs in disk space, adjust it,
press Download, and Primer fetches, checks and configures everything itself.

Success: on a fresh copy with no models, Setup suggests "Hybrid, 12 GB GPU" from the hardware
detection, lists about 28 GB of items against the free space on the chosen drive, downloads them
with progress (resuming after interruptions), verifies each against the publisher's hash, and
writes a ready-to-use tier. Primer starts its servers from it, and the first page works with no
config editing.

## 2. Goals

- **A Setup window** (dock: a "download" glyph; opened automatically on first run when no tier
  works). Sections: the **kit**, the **destination** (a folder; free space shown), **progress**,
  and **installed items**.
- **Kits per tier**, suggested from the existing hardware detection:
  - *Light* (CPU or small GPU): LFM3-1.7B for writing and the fast jobs, LFM3-Audio-2B (voice),
    top-50k Wikipedia "mini", a world map to zoom 8, llama.cpp CPU build, kiwix-tools.
  - *Hybrid* (8–16 GB GPU): gemma-4-E4B (writing, GPU), LFM3-1.7B (fast, CPU), Qwen-Image 2.1 (with
    its text encoder and VAE), LFM3-Audio-2B, top-50k Wikipedia nopic, the world map to zoom 9,
    llama.cpp and stable-diffusion.cpp CUDA builds, kiwix-tools. About 28 GB.
  - *Workstation* (24 GB+): as Hybrid plus Wan 2.2 TI2V 5B video, every-article Wikipedia
    ("mini"), and larger quants.
  Each item shows its name, size, licence and source, can be **skipped** or **swapped** for another
  quant or variant from the same publisher listing, and is pre-ticked off when already present.
- **Live catalogues** (all plain HTTPS, standard library):
  - Hugging Face API: file lists with sizes and SHA-256 (LFS), the model card, the licence
    (`cardData.license`), and whether the repo is gated.
  - Kiwix's catalogue (OPDS) and download.kiwix.org: ZIMs with language, size and date, and their
    published SHA-256.
  - GitHub releases: llama.cpp, stable-diffusion.cpp builds (CUDA, Vulkan or CPU, chosen from the
    hardware detection), with the asset SHA-256 GitHub publishes. kiwix-tools from
    download.kiwix.org (MD5 only is published; shown as such).
  - The map through `tools/map_extract.py` (Phase 5), with a zoom choice and its size estimate;
    the fonts from Protomaps' basemaps-assets.
- **Downloads**: one at a time in the background, resumable (HTTP ranges, `.part` files),
  verified against the published hash before being moved into place, with speed and time left.
  Pause, resume and cancel. Survive a Primer restart (resume where they left off).
- **Configure itself**: when a kit finishes, Primer writes a tier `tiers/local-<kit>.toml`
  (git-ignored) with every path, port and setting the docs recommend (LFM3 thinking off, voice
  ports, map and reference sections), runs the Models scan for picture/video models, and offers to
  switch to the new tier.
- **Existing files count**: items already on disk (found by name and size, or by hash when asked)
  are not downloaded again. llama-server found on the PATH (e.g. a winget install) can be used
  instead of downloading.
- **Move models**: list the configured model files with their drive, flag ones on removable or
  slow drives, and copy chosen files to another folder (verifying the copy, keeping the original),
  updating the paths in the tier and `var/images.json`.
- **Updates**: a "check for updates" action lists newer ZIMs and newer runtime builds; nothing is
  ever updated automatically.

## 3. Safety

- **Download allowlist** for the first request: `huggingface.co`, `download.kiwix.org`,
  `library.kiwix.org`, `github.com` / `api.github.com` (release assets), `build.protomaps.com`,
  `codeload.github.com` (basemaps-assets). Redirects are followed to any HTTPS host (publishers use
  CDNs and mirrors), and integrity comes from the published hash, which must match before a file is
  used.
- **Formats**: `.gguf`, `.safetensors`, `.zim`, `.pmtiles`, and release `.zip`s. Pickle formats
  (`.ckpt`, `.pt`, `.bin`, `.pth`) are refused, as everywhere else.
- **Archives** are extracted safely: no absolute paths, no `..`, no symlinks, size-capped.
- **Executables** (from release zips): Primer shows the publisher, release, file and SHA-256, and
  runs nothing until the user presses **Approve**. The approval is recorded by hash; a different
  file at the same path is not approved. Executables the user configured by hand keep working as
  before.
- **Licences**: each model shows its licence from its card; downloading needs the licence
  acknowledged once per licence (Gemma's terms differ from Apache 2.0, LFM's "other" licence
  links to its text). Gated repos need a Hugging Face token in `var/keys/huggingface.key`
  (`api_key_file` style), never sent to any other host.
- **Disk space**: a kit can't start if it doesn't fit (with a margin), and a download stops
  cleanly when the disk fills.
- Setup's routes follow the same origin rules; downloads only ever write inside the chosen
  destination folder.

## 4. Decisions (Claude's calls)

- **Kits are a curated manifest** in the repo (`primer/setup/kits.json`): each item names its
  source (Hugging Face repo + file pattern, a Kiwix book name + flavour, a GitHub repo + asset
  pattern, a map zoom), its role (text, fast, image parts, video parts, voice, reference, map,
  runtime) and a default choice. Exact files, sizes and hashes are resolved live from the
  catalogues when the kit is shown, so the manifest doesn't go stale.
- **Destination layout**: `<root>/models/<publisher>/<repo>/…`, `<root>/reference/…`,
  `<root>/maps/…`, `<root>/tools/<name>/<release>/…`. State in `var/setup.json` (items, progress,
  approvals, licences acknowledged).
- **Tier writing**: a small TOML writer for the tier structure Primer reads (no third-party TOML
  library); the tier is regenerated, never edited in place, so a user's own tiers are untouched.
- **Removable-drive check** on Windows via `GetDriveTypeW` (ctypes, standard library); elsewhere,
  the mount type where cheaply known.

## 5. Non-goals

- No automatic updates, telemetry, or accounts.
- No installing system packages, drivers or CUDA itself (the CUDA runtime DLLs ship in the
  llama.cpp/sd.cpp CUDA zips).
- No arbitrary-URL downloads from the UI.

## 6. Left to the builder

The exact kit contents and quants (verified against the live catalogues when building), the
window layout, and test structure.
