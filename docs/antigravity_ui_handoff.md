# Antigravity — UI work handoff

> **SUPERSEDED 2026-08-14.** Antigravity's role changed from frontend
> *implementer* to *designer*: it produces specifications, not code.
> **Current brief: `docs/antigravity_design_brief.md`.**
>
> Kept because the invariants and house rules below still hold and each came
> from a bug that shipped — the design brief carries them forward. What is out
> of date is the role, the build instructions, the phase status and the
> component line counts.


Written 2026-08-05. Claude Code owns the backend, API contracts, persistence and
deployment. This document is the current contract; where anything here conflicts
with an older instruction, this wins.

**Your scope is the frontend.** The backend for everything described below is
built, deployed and verified. Do not add backend routes — if a UI needs data
that no endpoint returns, say so and it will be added rather than worked around.

---

## The one architectural rule

**Rendering happens on the server. The browser never composites.**

There is no second compositor and there must not be one. A browser renderer
would drift from the real one and cannot reproduce the depth-warp parallax, so
any preview built in-browser would be a confident lie about what ships. The
timeline lays out what the server *will* render; the video element plays what the
server *did* render. Nothing in between.

If a design seems to need in-browser compositing, the answer is a server render
plus a job, not canvas work.

---

## Where things stand

Phases 1–6 are complete. Studio: https://youtube-video-pipeline-mfelaj54qa-uc.a.run.app

| Component | Lines | Owns |
|---|---|---|
| `page.tsx` | 1059 | all state + all handlers (deliberately) |
| `MultitrackTimeline.tsx` | 482 | Step 3 — tracks, playhead, clip inspector |
| `FlowCanvas.tsx` | 389 | Step 1 — beat + audio node graph |
| `MetadataPanel.tsx` | 254 | Step 5 — publishing copy, export |
| `MotionPanel.tsx` | 311 | Step 4 — parallax |
| `AssemblyPanel.tsx` | 213 | pipeline control board |
| `AudioNode.tsx` | 130 | one audio layer as a node |
| `StepHeader.tsx` / `GradePanel.tsx` / `MixPanel.tsx` | 127 / 126 / 108 | steps, look, bus levels |

The five steps gate on real state from `/api/project/active` → `counts`,
`script_locked`, `storyboard_approved`.

---

## What to build

### 1. Draw one A2 block per SFX layer  ← highest value

A beat can now carry several SFX layers. The timeline still renders **one** A2
block per beat, so a three-layer beat looks like one sound. That is actively
misleading — it is worse than showing nothing.

Each shot in `/api/project/active` carries:

```jsonc
"sfx_layers_resolved": [
  { "id": "legacy", "label": "", "prompt": "wind through sawali",
    "source": "generated", "gain": 1.0, "offset": 0.0,
    "fade_in": 0.0, "fade_out": 0.0, "url": "audio/sfx/s003.mp3" }
]
```

Give each layer its own lane under A2 (A2a, A2b, …), positioned at
`beat.start + layer.offset`. Note **offset may be negative** — a layer can start
under the previous beat, and one on the first beat can start before 0:00. Do not
clamp it in the UI; the mix trims the head server-side.

### 2. Make those blocks draggable to set `offset`

The backend already accepts it. Drag horizontally, commit on release:

```
POST /api/shot/{scene_id}/layers   { "id": "<layer_id>", "offset": -3.0 }
```

Same for narration, which lives on the shot rather than in a layer:

```
POST /api/shot/{scene_id}   { "offset_narration": -1.5 }
```

### 3. Show fades on the blocks

`fade_in` / `fade_out` are seconds. Draw them as ramps at the block edges —
ideally draggable, but showing them is most of the value.

### 4. Layer creation in the timeline

`AudioNode` can generate, upload and delete layers; the timeline cannot *add*
one. A "+ layer" affordance on A2 calling `POST /api/shot/{id}/layers` with a
`prompt` would close the loop.

---

## API you will need

All non-GET requests require `X-Studio-Key`. `page.tsx` already has `post()` and
`postFile()` helpers that attach it and prompt on 401 — use those, do not call
`fetch` directly for writes.

```
GET  /api/project/active     project, shots, counts, mix, grade, motion,
                             preview_url, preview_meta
GET  /api/audio/peaks        { scene_id: { narration: number[], sfx: number[] } }
                             240-bucket 0..1 envelopes, cached server-side

GET  /api/shot/{id}/layers                    list with playable urls
POST /api/shot/{id}/layers                    create (no id) or sparse edit (id)
POST /api/shot/{id}/layers/{lid}/generate     render from that layer's prompt
POST /api/shot/{id}/layers/{lid}/delete       unlink from the mix, file kept
POST /api/shot/{id}/layers/upload             multipart, mp3/wav/m4a/ogg/flac
POST /api/shot/{id}                           camera, grade, gains, offsets, fades
POST /api/motion/preview/{id}                 re-render ONE beat (~1 min)
```

### `preview_meta` — read this before touching the playhead

```jsonc
{ "runtime": 334.26, "live_runtime": 334.26, "stale": false,
  "beats": [ { "scene_id": "s001", "start": 0.0, "duration": 16.87 } ] }
```

The playhead **must** map video time through `preview_meta.beats`, not through
the live manifest durations the tracks are drawn from. The MP4 was rendered from
whatever the durations were at build time; using live values makes the playhead
point at the wrong beat the moment anything is retimed, and say nothing about it.
`stale: true` means the two have diverged — surface it, do not hide it.

`preview_meta` is `null` for previews built before this existed. Render no
playhead rather than a wrong one.

---

## House rules

These each come from a bug that actually shipped here.

**Commit on release, never on change.** A range input fires `onChange` per pixel
of travel, and every write is `save_current_project` → a GCS round trip. One
slider drag was ~100 writes. Use `onPointerUp` / `onBlur`, with local state while
dragging. Same for number fields: `defaultValue` + `onBlur`, not `value` +
`onChange`.

**Send only the field that changed.** `POST /api/shot/{id}` takes sparse objects.
A handler that sent a whole `camera` object with `move` hardcoded silently reset
every pan beat to `push_in` when a duration was dragged.

**Never hide a control; explain it.** A locked step, an inert panel, a missing
clip — all say *why*. Controls that vanish read as breakage, and this UI has lost
controls twice that way.

**Distinguish "not generated" from "none for this beat".** They need different
actions, and collapsing them wasted debugging time.

**Only real Tailwind scale values** — 50, 100–900, 950. A previous pass used
`zinc-450/550/650/750/850` in 39 places; those render as no style at all.

**All state and handlers stay in `page.tsx`.** Extract presentation only. Moving
state is what turns a refactor into a regression, and both surfaces that can
regenerate audio call the *same* handler on purpose — "regenerate" must mean one
thing wherever it is pressed.

---

## Do not

- Build a browser compositor, or any preview not produced by the server.
- Add a second endpoint for something an existing one does. Two write paths to
  one piece of state is the pattern behind two live bugs here.
- Add YouTube upload. Out of scope by decision; publishing is not in this pipeline.
- Change backend files. If you need a contract change, ask.
- Migrate manifests. Old shapes are handled at read time on purpose —
  `sfx_layers: []` falls back to the legacy single file.

---

## Verifying

```
cd frontend && npx tsc --noEmit && npm run build     # both must pass clean
```

Deploy is Claude Code's: `gcloud builds submit --config cloudbuild.yaml .` is the
only supported path.

Test against the Manananggal project — 15 beats, all audio generated, all steps
unlocked. A fresh empty project is the other case worth checking: Step 1 only,
Steps 2–5 locked and each stating why.
