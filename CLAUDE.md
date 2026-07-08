# Codebase Rules — Deep Root Lore Pipeline

Binding rules for all code in this repository. This is a **generative dark-folklore
video production pipeline** ("Deep Root Lore"): a zero-stock, modular Python
post-production engine that cuts human work from ~10 hours to a 30–60 minute
review/finesse loop per video.

**Cost discipline is a first-class design goal:** target ~$15–25 per finished
10-minute video (never the ~$100 you get from animating every shot with a paid
video model). Quality comes from a trained style LoRA + 2.5D parallax, *not* from
paying to render everything.

**Visual identity:** ink-and-watercolor graphic-novel horror — heavy cross-hatch
linework, painterly washes, paper grain, vignette borders; tight chiaroscuro
palette (near-black shadow, warm amber candlelight, desaturated cool exteriors);
cinematic 16:9.

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
| `assets.py` | fal.ai media: Tier-1 flux draft variations (LoRA) + Tier-2 gated video |
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

- Images are generated through a **trained flux LoRA** ("Deep Root Lore") with a
  locked trigger word + fixed per-character seed to prevent drift.
- Training frames live in `lora_training/` (gitignored).
- **Shadow-play silhouette** (e.g. the manananggal on the bamboo wall) is a
  first-class, recurring shot type: cheapest tier, always on-model, maximally eerie.

## Audio

- Music is **source-agnostic**: the pipeline consumes whatever WAV/MP3 sits in
  `audio_pool/` (Pixabay / Suno / Epidemic Sound — user-curated, monetization-safe,
  fully owned or licensed). librosa analyzes the selected track.

## fal.ai model IDs

- Draft (Tier 1): `fal-ai/flux-lora` (with trained LoRA) — fallback `fal-ai/flux/schnell`
- Video (Tier 2): `fal-ai/kling-video/v3/image-to-video` or `fal-ai/bytedance/seedance-2.0`
