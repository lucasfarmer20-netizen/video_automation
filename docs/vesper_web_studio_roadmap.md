# Vesper Web Studio — UI/UX Roadmap (revised)

**Repository**: `c:\Users\Lucas_Admin\video_automation`
**Goal**: turn the storyboard dashboard into a 4-step in-browser production suite.

> **Status of this document.** This is a corrected revision of the earlier
> handoff, which was written against commit `520fd1c` and has since gone stale in
> ways that would produce broken code. Verified against commit `80d2f94`,
> Cloud Run revision `youtube-video-pipeline-00133-56q`. Where the previous
> version disagrees with this one, this one was checked against the running code.

---

## 0. Verified system state

Checked, not assumed:

| Area | Actual |
|---|---|
| LLM | `claude-opus-5` (`backend/script.py: DEFAULT_MODEL`), structured outputs enforced via `SCRIPT_SCHEMA`, `max_tokens=32000`, streaming |
| Image backends | 9, registered in `assets.IMAGE_BACKENDS`. Default `nano2` (Nano Banana 2 / Gemini 3 Pro Image) |
| Video backends | `seedance_2_0`, `veo_3_1`, `kling_2_5_turbo_pro`, `wan_2_7`, `luma_dream_machine`, `hunyuan_video` |
| Voice | ElevenLabs TTS + Voice Design |
| SFX | `fal-ai/stable-audio` |
| Depth | Depth-Anything V2 ONNX at `/gcs/models/…`, staged to local disk at first use; heuristic fallback if absent |
| Persistence | **GCS JSON manifests only.** Firestore is coded but *no database is provisioned* — every call throws and is swallowed |
| Auth | `STUDIO_API_KEY` set in production; **every mutating request needs `X-Studio-Key`** |
| Runtime | 2 vCPU / 4 GiB, `--no-cpu-throttling` (background threads die without it) |

### Corrections to the previous handoff

- `_parse_json_robust()` **no longer exists.** It masked 4096-token truncation by
  appending braces, silently yielding short storyboards. Replaced by enforced
  structured outputs. Do not reintroduce JSON repair.
- Default LLM was `claude-3-5-sonnet-20241022` — **retired since 2025-10-28**, so
  every draft 404'd before falling through a 7-model retry chain.
- Default image backend is `nano2`, not `flux-2-pro`.
- The "one-click Dismiss Error" was broken: the 3-second poll replaced job state
  wholesale and resurrected the banner. Dismissals are now tracked separately.

### Verification before any commit

```bash
python -m pyflakes backend/*.py pipeline.py     # cross-file undefined names
python -c "import backend.main, pipeline"       # actually resolves imports
cd frontend && npm run build
```

`py_compile` is **not** sufficient. It compiles without importing:
`backend/characters.py` imported a deleted symbol, compiled clean, and failed at
runtime with `ImportError`.

### Deployment — one path only

```bash
gcloud builds submit --config cloudbuild.yaml .
```

Do **not** use `gcloud run deploy --source .`. It omits `--no-cpu-throttling`,
`--cpu`, `--memory` and `DEPTH_MODEL`. Those survive on an existing service by
implicit preservation, but `--no-cpu-throttling` is what stops Cloud Run freezing
background threads mid-generation and silently discarding work. `cloudbuild.yaml`
carries every flag; keep it the only source of deploy config.

---

## 1. API reference

Complete route list, from the running app. **Every `POST` requires
`X-Studio-Key: <STUDIO_API_KEY>`** when that variable is set (it is, in production).
`GET` requests are unauthenticated.

### Project

| Method | Path | Payload |
|---|---|---|
| `GET` | `/api/projects` | — (optional `?channel=`) |
| `GET` | `/api/project/active` | — |
| `POST` | `/api/project/select` | `{rel: string}` — absolute manifest path inside the workspace |
| `POST` | `/api/project/new` | `{name: string, channel: string}` |
| `POST` | `/api/project/meta` | `{title?: string, channel?: string}` |

### Script

| Method | Path | Payload |
|---|---|---|
| `POST` | `/api/script/generate` | `{topic: string, beats?: number, channel?: string}` |
| `POST` | `/api/script/from_chat` | `{messages: Message[], beats?: number, channel?: string}` |
| `POST` | `/api/script/lock` | — |
| `POST` | `/chat/develop` | `{messages: Message[], channel?: string}` |

### Beats

| Method | Path | Payload |
|---|---|---|
| `POST` | `/api/shot/{scene_id}` | see accepted fields below |
| `POST` | `/api/regenerate/{scene_id}` | `{backend?: string}` — paid |
| `POST` | `/api/shot/{id}/edit_image/{var_idx}` | `{prompt: string, backend?: string}` — paid |
| `GET` | `/api/shot/{id}/video_quote` | `?video_model=` — what the generation below will cost |
| `POST` | `/api/shot/{id}/generate_video` | `{accepted_cost: number, video_model?: string}` — **paid; `accepted_cost` is required and must be the figure `video_quote` just returned** |
| `POST` | `/api/shot/{id}/image` \| `/clip` \| `/reference` | multipart `file` |
| `POST` | `/api/shot/{id}/delete_image/{i}` \| `/delete_video/{i}` | — |
| `POST` | `/api/shot/{id}/chat` | `{messages: Message[]}` |
| `POST` | `/api/shot/{id}/apply_chat_prompts` | `{refined_prompt?, refined_motion_prompt?}` |
| `POST` | `/api/shot/{id}/reference/remove` | `{name: string}` |

**`/api/shot/{scene_id}` accepts exactly these fields — anything else is silently ignored:**

```
chosen_variation, chosen_video_variation, motion_type, video_model,
video_audio, narration, prompt, style_medium, motion_prompt, flow_hero
```

`camera` is **not** accepted. See Phase 0.

### Pipeline

| Method | Path | Payload |
|---|---|---|
| `POST` | `/api/approve` | — refuses unless every beat has `draft_image` |
| `POST` | `/api/assemble/{stage}` | **no body.** `stage` ∈ `drafts \| narration \| render \| preview \| timeline`; `render` takes `?accepted_cost=` (see below) |
| `GET` | `/api/render/quote` | `?force_paid=` — the paid video a whole-episode render would buy, beat by beat |
| `GET` | `/api/assemble/status` | — returns `{jobs: {name: {status, log}}}` |
| `POST` | `/api/render` | render knobs (backend, guidance_scale, nag_scale, …) |
| `POST` | `/api/render/reference` \| `/clear` | multipart `file` / — |
| `POST` | `/api/voice/design` | `{gender, age, accent, description, sample_text?}` |
| `POST` | `/api/voice/settings` | `{voice_id?, stability?, style_exaggeration?}` |
| `POST` | `/api/audio/sfx/{scene_id}` | — paid |

`/api/assemble/render` takes **no** `{scenes: []}` filter; it processes every beat.

**Paid video is quoted before it is bought.** `/api/assemble/render` and
`/api/assemble/rough_cut` require `?accepted_cost=` whenever they plan to buy any,
and it must equal the total `GET /api/render/quote` reports. A request that names no
price is refused before the job starts — nothing is compiled and nothing is charged.
The confirmed total is then spent down one beat at a time at the dispatch itself, so a
beat the quote did not predict (one re-timed to its narration, say) is refused rather
than bought. Gate 1 is still checked and is not a substitute: approval says a render
budget was allocated, not that anyone agreed to this charge.

### Media

`GET /media/{path}` — the only media route the frontend should use. Paths from the
API are relative to a media root (`assets/s001/x.png`, `render/<slug>/s001.mp4`)
and are **never** route-prefixed. `mediaUrl()` adds `/media/` exactly once. Paths
are containment-checked and restricted to an extension allowlist.

---

## 2. Target: 4-step workflow

```
[ 1. Script & Beats ] ➔ [ 2. Audio & Voice ] ➔ [ 3. Editor ] ➔ [ 4. Export ]
```

---

## Phase 0 — Prerequisites (blocking)

**Nothing in Phases 2–4 should start before these.**

### 0a. Prove the pipeline end-to-end

`narration`, `render`, `preview` and `timeline` have **never executed
successfully**. Everything below assumes they work. Run one storyboard
(Manananggal, 15 beats, 10 parallax / 4 static / 1 `ai_video`) all the way to
FCPXML first. Build UI against observed behaviour, not assumed behaviour.

### 0b. Make `camera` writable

Phase 2's headline interaction — drag-to-trim — writes `shot.camera.duration`.
`/api/shot/{id}` does not accept `camera`, and `FlowCanvas` already calls:

```ts
onUpdateDuration={(sceneId, dur) => handleUpdateField(sceneId, "camera", {...})}
```

The POST returns `ok: true`, the frontend optimistically updates local state, and
the server discards the write. It **looks** like it works. Add a `camera` branch
to `update_shot` with validation (`duration > 0`, `move` in the known set) before
building any timeline UI.

### 0c. Decide who owns `camera.duration`

`audio.sync_durations()` overwrites `camera.duration` from narration length
(VO + pad). Trimming a clip and then running narration silently discards the trim.
Pick one:

- **Narration wins** — trimming only meaningful after narration; UI should say so.
- **`duration_locked: bool` per beat** — `sync_durations` skips locked beats.

The second is better for an NLE, and needs a manifest field plus a UI affordance.

---

## Phase 1 — Step header & workflow router

Create `frontend/src/components/StepHeader.tsx`, integrate into `page.tsx`.

**Five steps, per the `step2_audio_studio.jpg` mockup** (decided 2026-08-01; the
mockups disagreed with each other — `step1`/`step4` showed four steps, `step3`
showed a different four, `step2` showed five. Five wins):

| # | Step | Gate |
|---|---|---|
| 1 | Pre-Production | — |
| 2 | Audio Studio | `script_locked` |
| 3 | Editing | `storyboard_approved` |
| 4 | Visual FX | rendered beats exist |
| 5 | Final Review | preview or FCPXML exists |

- `const [activeStep, setActiveStep] = useState<1|2|3|4|5>(1)`
- Five pills; active in amber `#f59e0b`.
- **Gate steps on real state**, don't just navigate. A locked step should say
  *why* — the existing assembly panel does this and it's the pattern to follow.
  Never hide a control silently; that reads as "my controls disappeared."

Step 4 (Visual FX) is where the parallax controls live — project defaults from
`GET/POST /api/motion`, per-beat override on `camera.amount` / `camera.speed`,
single-beat re-render via `POST /api/motion/preview/{scene_id}`.

**The node editor is in scope** (decided 2026-08-01). `FlowCanvas` already
exists, so this is a visual upgrade to match `00_dashboard_overview.jpg` — beat
nodes joined by bezier curves with model badges — not new plumbing. It belongs
in step 1 (Pre-Production), alongside the storyboard beats.

**Not in scope:** the "Export & Publish to YouTube" button in
`step4_export_publishing.jpg`. Publishing is out of this phase; build the
server-side master render and the FCPXML export, and leave the publish control
out rather than ship a dead button.

`page.tsx` is ~1000 lines before this. Extract the existing assembly panel and
job banners into components as part of Phase 1, or Phase 2 lands on top of a file
that's already hard to work in.

---

## Phase 2 — Multitrack timeline

`frontend/src/components/TimelineEditor.tsx`. **Requires Phase 0b and 0c.**

- **V1** — thumbnails, width ∝ `camera.duration`, drag handles, model pill from
  `shot.image_model || project.render.backend`, camera-move tag.
- **A1 narration** — emerald `#10b981`.
- **A2 SFX** — amber `#f59e0b`.
- **A3 music** — purple `#8b5cf6`.

Notes:

- **Desktop-only.** `page.tsx` has substantial mobile drawer work; a multitrack
  editor will not survive a 375px viewport. Below `lg`, show the existing beat
  grid instead of a broken timeline.
- Waveforms need real audio. Narration mp3s are at
  `audio/<slug>/narration/<scene>.mp3` — served only if under a media root.
  Confirm reachability via `/media/` before designing around waveform rendering;
  otherwise draw fixed-width blocks from duration.
- Debounce trim writes. Each one is a `save_current_project` → GCS write.

---

## Phase 3 — Preview (revised approach)

**Recommendation: do not build a browser compositor.**

The original plan called for a WebAudio + Canvas player compositing narration,
SFX and music with simulated Ken Burns and parallax. Three problems:

1. `timeline.build_preview()` already produces an accurate `_preview.mp4` —
   ffmpeg concat plus a mixed audio bed. A browser compositor is a **second
   implementation of the same thing** and will drift from it.
2. **It cannot reproduce parallax.** `motion.py` does a per-pixel depth warp using
   Depth-Anything V2. A CSS/Canvas Ken Burns approximation is a different image.
   You would art-direct against a preview that misrepresents your primary visual
   tier (~70% of shots).
3. Sample-accurate sync of three audio tracks against a 24 fps canvas is hard and
   will jitter.

**Instead**: a scrubbable player over the real `_preview.mp4`, with the Phase 2
timeline overlaid and synced to `video.currentTime`. Click a beat → seek. Playhead
follows playback. Accurate by construction, and a fraction of the work.

If faster iteration is needed, make the *server* preview cheaper (lower
resolution, fewer beats) rather than approximating it client-side.

---

## Phase 4 — Export & publish (split)

### 4a. Metadata generator — do this

No auth, no quota, genuinely useful. Generate from the locked script: SEO title,
description, chapter timestamps from cumulative `camera.duration`, tags. Add a
`/api/publish/metadata` endpoint calling Claude with the storyboard as context.

### 4b. Export — do this

Buttons for `/api/assemble/preview` and `/api/assemble/timeline`, plus a download
for the `.fcpxml`. Thin wrappers over endpoints that already exist.

### 4c. YouTube upload — decide before building

Two obstacles:

- **It contradicts Gate 2.** CLAUDE.md: *"timeline.py emits an FCPXML for DaVinci
  Resolve; the human finishes the cut there. The pipeline never auto-renders a
  final master."* One-click publish is the opposite. That's a product decision,
  not an implementation detail — make it deliberately.
- **It is not a button.** YouTube Data API upload needs OAuth 2.0 with refresh
  token storage, and costs **1600 quota units per upload** against a default
  10,000/day — roughly six uploads daily. Non-test users need app verification.

Treat 4c as its own project with its own design doc.

---

## 3. Cross-cutting requirements

### Cost guards

Every paid action needs a scoped confirm quoting real numbers, as
`/api/assemble/drafts` does (*"3 variations for 25 beats — 75 images, roughly
$11.25"*). An NLE makes re-rendering one click; without guards this becomes the
main way money is lost. Rough figures: stills ~$0.15/image ×3 per beat; video is
per-clip and model-dependent; local `parallax`/`static` are $0.

### Auth

Route every mutating call through the existing `post()` / `postFile()` helpers in
`page.tsx`. They attach `X-Studio-Key`, prompt once on 401, and replay. Do not
hand-roll `fetch` for mutations.

### Long jobs

`/api/assemble/{stage}` returns immediately; poll `/api/assemble/status`. The job
registry lives in process memory, so a cold start empties it — a running job can
vanish from the UI. Treat "job disappeared" as *unknown*, not *failed*.

### Persistence

Firestore is coded but **not provisioned** — every call throws and is swallowed.
GCS JSON manifests are the only store. Do not build features assuming Firestore
queries, transactions or subcollections work. Either provision it deliberately or
strip the dead code; leaving it half-wired is how `get_current_project` ends up
with two disagreeing sources of truth.

---

## 4. Design system

- Dark, `bg-zinc-950`.
- Accents: amber `#f59e0b`, emerald `#10b981`, blue `#3b82f6`, purple `#8b5cf6`.
- Glass: `backdrop-blur-xl`, `border-zinc-800`, `shadow-2xl`.
- Monospace for metadata badges.

**Only use real Tailwind scale values** — 50, 100–900, 950. A previous pass used
`zinc-450/550/650/750/850` in 39 places; those aren't on the scale and render as
no style at all, which looks like a layout bug rather than a missing colour.

---

## 5. Suggested order

1. **Phase 0a** — one storyboard end-to-end. Blocks everything.
2. **Phase 0b/0c** — `camera` endpoint + duration ownership.
3. **Phase 1** — step header, plus extracting `page.tsx` components.
4. **Phase 4a/4b** — metadata + export. Cheap, high value, no new infrastructure.
5. **Phase 2** — timeline.
6. **Phase 3** — scrubbable player over the real preview.
7. **Phase 4c** — only after an explicit decision on Gate 2.

4a/4b are ahead of 2 deliberately: they deliver a finishable video sooner, and
they exercise `timeline`/`preview` — the least-tested stages — while the surface
area is still small.
