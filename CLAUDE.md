# Codebase Rules — The Illuminated Bestiary Pipeline

Binding rules for all code in this repository. This is a **generative dark-folklore
video production pipeline** ("The Illuminated Bestiary"): a zero-stock, modular Python
post-production engine that cuts human work from ~10 hours to a 30–60 minute
review/finesse loop per video.

**Cost discipline is a first-class design goal:** target ~$15–25 per finished
10-minute video (never the ~$100 you get from animating every shot with a paid
video model). Quality comes from a trained style LoRA + 2.5D parallax, *not* from
paying to render everything.

**Visual identity:** documentary folklore horror — each entity is illustrated in a
**historical art medium authentic to its culture of origin** (e.g. Ukiyo-e
woodblock for Japanese yōkai, lubok / Bilibin for Slavic, illuminated-manuscript
codex for medieval European, Adinkra / Benin-bronze aesthetic for West African).
The medium *leads* the image prompt (style = prompt-medium-leading). Unifying
grammar across cultures: strong chiaroscuro, deep shadow, shadow-play silhouette
for reveals, cinematic 16:9 — never a modern digital / 3D / anime / photographic look.

## Runtime

- **Python 3.11+** is required.
- **ffmpeg is a required system-level install** — installed on the host OS, **not**
  via pip. Do not add it to `requirements.txt`.
- **Local ML targets AMD/Windows**: depth and inpaint run via ONNX Runtime
  (DirectML backend) or CPU. **Never assume CUDA / an NVIDIA GPU.**

## Architecture

- **Single orchestrator:** `pipeline.py` at the repo root is the only entrypoint.
  It routes to modular code in `src/`. `pipeline.py` orchestrates; it does not
  implement domain logic itself.
- **No monolith files** — one concern per module, kept small and unit-testable.
- **Strict, lightweight state file:** `storyboard_manifest.json` is the single
  source of run state. Its schema lives in `src/manifest.py`.

### Modules (`src/`)

| Module | Responsibility |
|---|---|
| `config.py` | Env/secret loading (`os.environ.get`) + derived path constants |
| `script.py` | Claude API script draft (anti-AI-tell system prompt); the **script gate** |
| `audio.py` | ElevenLabs narration (TTS) + **librosa analysis of the background MUSIC track** (transients, rhythm shifts, silent gaps) to anchor cuts |
| `assets.py` | fal.ai draft images: Tier-1 `flux-general` (NAG negative prompt); legacy nano / flux-lora backends |
| `depth.py` | Depth map → layer separation → gap inpaint (local, free) |
| `motion.py` | 2.5D parallax + procedural-FX render engine (moviepy/ffmpeg, local, free) |
| `dashboard.py` | Flask/HTML local UI = the storyboard/budget gate |
| `timeline.py` | OpenTimelineIO → DaVinci Resolve FCPXML, cuts mapped to librosa beats |

## Motion tiers (how we stay under budget)

Every shot carries a `motion_type`. Reserve the paid tier for ~8–12 hero shots.

- **A — `static`**: single still + procedural FX (candle flicker, smoke, dust,
  grain, vignette breathe). Local, **$0**.
- **B — `parallax`**: depth-sliced still, gaps inpainted, layers drift under a slow
  camera move. Local, **$0**. This is ~70% of shots and the "motion comic" look.
- **C — `ai_video`**: fal image-to-video for real-motion beats only. **Paid, gated.**

## Secrets & configuration

- **All secrets read via `os.environ.get`.** Never hardcode keys, tokens, or
  absolute paths anywhere.
- Required keys (documented in `.env.example`; real `.env` is gitignored):
  `ELEVENLABS_API_KEY`, `FAL_KEY`, `ANTHROPIC_API_KEY`.

## Gates (hard requirements — never bypassable)

1. **Script gate.** `script.py` drafts the narration; a human refines it; the
   script is **locked** before it becomes input to `audio.py`. Nothing downstream
   runs on an unapproved script.
2. **Storyboard / budget gate (Gate 1).** `pipeline.py` runs up to and including
   draft-image generation, then **pauses**, launches the Flask dashboard, and
   **refuses to call any paid video API** until an approved
   `storyboard_manifest.json` is written — with per-shot approval *and* each
   shot's `motion_type` set (this is where the human allocates the render budget).
   The Tier-C stage is unreachable until the gate is cleared.
3. **DaVinci assembly gate (Gate 2).** `timeline.py` emits an FCPXML for DaVinci
   Resolve; the human finishes the cut there. The pipeline never auto-renders a
   final master.

## Style consistency

- **Consistency comes from the WRAPPER, not a single house style.** Every episode
  shares a universal **manuscript / codex frame** (the book-turning intro, archival
  page + title system) and **Vesper's narration voice** — that constant is the
  channel's throughline. *Inside* that frame, each entity's interior shots transform
  into the **historical art medium authentic to its culture** (`Storyboard.cultural_origin`
  → per-beat `Shot.style_medium`), so variety across cultures never reads as
  inconsistency.
- Draft images use **`fal-ai/flux-general` (FLUX.1 [dev])**: `style_medium` leads the
  positive prompt; a shared **negative prompt** aggressively strips modern 3D renders,
  anime textures, and digital artifacting, applied via **NAG** (`nag_scale`,
  `num_inference_steps=28`, `guidance_scale=3.5`). NAG negates on this distilled model
  without real CFG — `use_real_cfg: true` + a negative prompt currently 422s the endpoint.
- **DEPRECATED — do not use for new work:** the trained ink LoRA `lora_config.json`
  (`DEEPROOTLORE`) and its trainer `scripts/train_lora.py`, plus the Nano-Banana
  style-transfer path. Retained only as `--backend flux-lora` / `nano` fallbacks; the
  single-locked-style approach is superseded by the per-culture-medium model above.
- **Shadow-play silhouette** remains a first-class shot type: reads as on-model in
  any culture's medium, cheapest tier, maximally eerie.

## Audio

- Music is **source-agnostic**: the pipeline consumes whatever WAV/MP3 sits in
  `audio_pool/` (Pixabay / Suno / Epidemic Sound — user-curated, monetization-safe,
  fully owned or licensed). librosa analyzes the selected track.

## fal.ai model IDs

- Draft (Tier 1): `fal-ai/flux-general` — FLUX.1 [dev]; `negative_prompt` honoured via
  NAG (`nag_scale`). Legacy: `fal-ai/flux-lora` (trained LoRA),
  `fal-ai/nano-banana/edit` (style-transfer), `fal-ai/flux/dev`
- Video (Tier 2): `fal-ai/kling-video/v3/image-to-video` or `fal-ai/bytedance/seedance-2.0`
