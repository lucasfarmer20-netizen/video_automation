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
| `assets.py` | fal.ai draft images: default `nano2` (Gemini 3 Pro Image); fallback `flux-general` (NAG); legacy nano / flux-lora |
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
3. **Assembly gate (Gate 2).** The human reviews the assembled cut before it
   ships. Two finishing routes, both available from the same approved manifest:
   - **In-studio master.** The web studio is the editor: trim, reorder, choose
     takes, set the music bed and SFX in the browser, then render the finished
     master server-side. The browser is the control surface; **rendering always
     happens on the server** from the manifest — there is deliberately no second
     compositor in the browser, because it would drift from the real renderer and
     cannot reproduce the depth-warp parallax.
   - **FCPXML export.** `timeline.py` still emits OTIO + FCPXML so the cut can be
     finished in DaVinci Resolve instead. Downloadable from `/api/export/{kind}`.

   What remains hard: **nothing renders or publishes without an approved
   storyboard**, and publishing to YouTube is not part of this pipeline.

## Style consistency

- **Consistency comes from the WRAPPER, not a single house style.** Every episode
  shares a universal **manuscript / codex frame** (the book-turning intro, archival
  page + title system) and **Vesper's narration voice** — that constant is the
  channel's throughline. *Inside* that frame, each entity's interior shots transform
  into the **historical art medium authentic to its culture** (`Storyboard.cultural_origin`
  → per-beat `Shot.style_medium`), so variety across cultures never reads as
  inconsistency.
- Draft images default to **`nano2` — Nano Banana 2 / Gemini 3 Pro Image**
  (`fal-ai/gemini-3-pro-image-preview`): a reasoning model with strong prompt
  adherence + character consistency. `style_medium` leads the positive prompt (plus
  the per-shot character anchors); it has no `negative_prompt` field, so the avoidance
  list is folded into the prompt. ~$0.15/image (2K).
- Cheaper fallback: **`flux-cfg` — `fal-ai/flux-general` (FLUX.1 [dev])**, where the
  negative prompt is applied via **NAG** (`nag_scale`), ~$0.04/image. (`use_real_cfg`
  + a negative prompt 422s that endpoint, so NAG — not real CFG — is used.)
- **DEPRECATED — do not use for new work:** the trained ink LoRA `lora_config.json`
  (`DEEPROOTLORE`) and its trainer `scripts/train_lora.py`, plus the Nano-Banana
  style-transfer path. Retained only as `--backend flux-lora` / `nano` fallbacks; the
  single-locked-style approach is superseded by the per-culture-medium model above.
- **Shadow-play silhouette** remains a first-class shot type: reads as on-model in
  any culture's medium, cheapest tier, maximally eerie.

## Audio

- Music is **source-agnostic**: the pipeline consumes whatever WAV/MP3 sits in
  the shared pool (`audio_pool/` locally, `/gcs/audio_pool/` deployed) — uploaded
  via `POST /api/music`, or **generated** via `POST /api/music/generate`.
  User-supplied tracks must be monetization-safe and fully owned or licensed.
  librosa analyzes the selected track.
- **Generated music** uses the registry in `audio.MUSIC_BACKENDS` (ElevenLabs
  Music, ACE-Step, Stable Audio 2.5, Cassette). None reaches a full episode
  runtime, which is fine — `timeline.py` loops the bed. Aim for a 2–4 minute
  loopable underscore, not a one-shot cue. The script stage writes the episode's
  `music_prompt`: sparse, instrumental, no percussion where a pulse would fight
  the narration.
- **SFX** are per-beat ambience (`Shot.sfx`) generated from the script stage's
  prompts via `fal-ai/stable-audio`. Environment only — room tone, weather,
  fire, water — never melody or instruments; the music bed is layered separately.

## fal.ai model IDs

- Draft (Tier 1, default): `fal-ai/gemini-3-pro-image-preview` — Nano Banana 2 / Gemini
  3 Pro Image (~$0.15/img). Cheaper fallback: `fal-ai/flux-general` (FLUX.1 [dev], NAG
  negative via `nag_scale`). Legacy: `fal-ai/flux-lora` (trained LoRA),
  `fal-ai/nano-banana/edit` (style-transfer), `fal-ai/flux/dev`
- Video (Tier 2): `fal-ai/kling-video/v3/image-to-video` or `fal-ai/bytedance/seedance-2.0`
