# Director API — Contract for the Frontend

**Backend status: Spike C + Planner + Critic implemented. Locked-coverage
generation deliberately NOT implemented.** Written 2026-08-09 against `main`.

This is the surface Antigravity builds against. Nothing here generates media,
spends money, or writes `storyboard_manifest.json`.

---

## 1. The shape in one line

**Scene-level planning in, per-beat coverage out.**

```
POST /api/director/plan   { beats: ["s004","s005","s006"] }
        │
        ├─ Planner  (one LLM call for the whole scene)
        ├─ Router   (application logic — picks models from capabilities)
        └─ Critic   (second LLM call, given the shot list WITHOUT the rationale)
        │
        ▼
   director/<beat_id>.json   status: "draft"     ← one file per beat
        │
   human reviews, revises, then locks
        │
        ▼
POST /api/director/lock/{beat_id}    status: "locked"
        │
        ▼
POST /api/director/compile/{beat_id}   ← EXISTS, but is Spike B (manual plans).
                                          Not wired to the planner. Generation
                                          from a locked plan is out of scope.
```

**Planning never generates.** A plan can be created, criticised, revised and
discarded without a single fal call. The only endpoint that spends money is
`compile`, which requires `status == "locked"` and is a separate human action.

---

## 2. Endpoints

### `GET /api/director/profiles`

Static reference data. Fetch once, cache. Drives every dropdown.

```jsonc
{
  "ok": true,
  "profiles": {
    "historical_docudrama": {
      "label": "Historical docudrama (Calluses)",
      "shot_seconds": [2.5, 5.5],
      "camera_motion": "restrained",
      "environmental_coverage": "high",
      "cutaway_density": "high",
      "face_exposure": "moderate",
      "max_ai_video_per_scene": 2,
      "note": "Observational. Hands, tools, environment and process over faces."
    },
    "documentary_illustrated": { … },
    "cinematic_documentary": { … }
  },
  "default_profile": "historical_docudrama",
  "vocabulary": {
    "purpose":     ["establishing","master","reaction","insert","cutaway","detail","transition"],
    "shot_size":   ["ews","ws","mw","m","mcu","cu","ecu"],
    "angle":       ["front","profile","three_quarter","rear_three_quarter","ots","high","low","overhead"],
    "camera_move": ["static","push_in","pull_out","pan_left","pan_right","tilt_up","tilt_down"],
    "motion_type": ["static","parallax","ai_video"]
  },
  "video_capabilities": [
    { "key": "seedance_2_0", "label": "Seedance 2.0 (image-to-video)",
      "allowed_durations": null, "min_seconds": 3, "max_seconds": 10,
      "cost_per_second": 0.10, "needs_start_image": true,
      "supports_reference_image": false, "supports_character_reference": false,
      "supports_extend": true },
    { "key": "veo_3_1", "allowed_durations": [4,6,8], "min_seconds": 4, "max_seconds": 8, … }
  ]
}
```

`vocabulary` is authoritative — **do not hardcode these lists in the frontend.**
Hardcoded copies of backend registries have drifted twice in this codebase, once
producing a dropdown entry (`kling_2_5_turbo_pro`) that silently rendered on a
different model.

---

### `POST /api/director/plan`

Starts a background job. Returns immediately.

```jsonc
// request
{
  "beats":    ["s004","s005","s006"],   // required; contiguous beats = one scene
  "profile":  "historical_docudrama",   // optional; falls back to default
  "notes":    "less dramatic, more environmental storytelling",  // optional, free text
  "critique": true                       // optional, default true
}

// 200
{ "ok": true, "started": true, "job": "director_plan:s004",
  "beats": ["s004","s005","s006"], "profile": "historical_docudrama" }

// 400 unknown beats  -> { "ok": false, "error": "unknown beats: ['s099']" }
// 409 already running -> { "ok": false, "error": "a plan for s004 is already running" }
```

**Poll `GET /api/assemble/status`** and read `jobs["director_plan:s004"]`
(`status`: `running` | `done` | `error`, plus a `log` string). Same job shape as
every other stage.

`notes` is where the natural-language creative controls land. "Fewer cuts", "more
environment", "show the protagonist less", "reduce cost" — send them as text; the
planner is instructed to honour them. No separate endpoint per control.

---

### `GET /api/director/scene?beats=s004,s005,s006`

The read model. Everything needed to render a scene view in one call.

```jsonc
{
  "ok": true,
  "beats": [
    {
      "beat_id": "s004",
      "beat_duration": 26.48,          // authoritative, from the manifest
      "coverage_total": 26.48,         // must equal beat_duration to compile
      "plan": {
        "beat_id": "s004",
        "beat_duration": 26.48,        // SNAPSHOT at plan time — see §5
        "plan_id": "scene-s004-s006",
        "scene_beats": ["s004","s005","s006"],
        "status": "draft",             // draft|locked|compiling|compiled|orphaned
        "profile": "historical_docudrama",
        "created_by": "planner",       // planner|manual
        "coverage": [ /* DirectorShot[] — see §3 */ ],
        "warnings": [ /* see §4 */ ],
        "compiled": {}                 // populated only after compile
      }
    }
  ],
  "summary": {
    "shots": 17, "paid_shots": 2, "estimated_cost": 3.15,
    "beats": [ { "beat_id": "s004", "shots": 6, "paid_shots": 1,
                 "estimated_cost": 1.35, "status": "draft" } ]
  }
}
```

`plan` is `null` when a beat has never been planned. That is the normal state and
is not an error.

---

### `POST /api/director/critique`  `{ "beats": [...] }`

Re-runs the critic over existing plans and rewrites their `warnings`. Synchronous.
Returns `{ ok, warnings, summary }`. Use after a human edits a plan.

### `POST /api/director/lock/{beat_id}?locked=true|false`

The human decision that a plan is worth producing.

```jsonc
{ "ok": true, "beat_id": "s004", "status": "locked" }
// 400 if the plan does not validate (durations do not sum to the beat)
// 409 if the beat is currently compiling
// 404 if there is no plan
```

Locking **validates first**, so a plan that cannot compile cannot be locked. A
locked beat is also protected from the ordinary render path.

### `GET /api/director/plan/{beat_id}`

Single-beat version of `scene`. Adds `problems: string[]` from validation.

---

## 3. `DirectorShot`

```jsonc
{
  "id": "s004.03",              // "<beat>.<2-digit ordinal>" — sorts lexically
  "beat_id": "s004",

  // editorial intent
  "purpose": "insert",
  "subject": "the accident report on the table",
  "shot_size": "cu",
  "angle": "overhead",
  "composition": "paper flat to camera, lamp light from the left",

  // timing + movement. DURATION LIVES IN camera, nowhere else.
  "camera": { "move": "push_in", "duration": 3.5, "speed": 0.75,
              "amount": 0.0, "duration_locked": false },

  // generation intent — describes NEEDS, never a model
  "character_motion": false,
  "face_visibility": "none",          // none|low|moderate|high
  "motion_complexity": "low",         // none|low|medium|high
  "gestural": false,                  // a movement that must complete; never trimmed
  "identity_critical": false,         // a face anchor; gets 4 takes instead of 1
  "reference_dependencies": [],

  // resolved by the ROUTER, not the model and not the frontend
  "motion_type": "parallax",          // static|parallax|ai_video
  "backend": "",                      // "" unless motion_type == ai_video

  "prompt": "…",                      // still image, scene only
  "motion_prompt": "…",               // what moves; empty for stills

  // library reuse
  "source": "generated",              // generated|library
  "source_ref": "",
  "library_scope": "",                // series|project

  // provenance — why this is not what was originally wanted
  "constrained_by": ["duration_quantized"],

  // produced state (all empty until compile)
  "draft_variations": [], "chosen_variation": null, "clip": "",
  "estimated_cost": 0.55, "error": ""
}
```

**Duration is `camera.duration` only.** There is no sibling `duration` field, and
adding one is how the timeline ended up with two time bases and a playhead
pointing at the wrong shot.

**`constrained_by` is the important one for UI.** It records that a technical
limit changed the shot, so a later re-plan can revisit exactly those decisions.
Current values:

| value | meaning |
|---|---|
| `duration_quantized` | the model's legal lengths forced a different duration |
| `will_trim_to_editorial_length` | generating longer, then trimming (ambient only) |
| `downgraded_no_legal_model` | wanted paid video, no model could serve it → parallax |
| `identity_reliability` | *(reserved — set once Spike A has measurements)* |

Show it. Six months on, nobody remembers whether a film is observational by
choice or because a model was weak in March.

---

## 4. Warnings

Concrete and checkable. **No composite score** — an authoritative-looking
percentage invites trust it has not earned.

```jsonc
{
  "beat_id": "s004",     // "" when it applies to the whole scene
  "shot_id": "s004.03",  // "" when not shot-specific
  "kind": "repeated_framing",
  "detail": "s004.02, s004.03 and s004.04 are all close-ups.",
  "suggestion": "Widen s004.03 to a medium to break the run."
}
```

`kind` ∈ `missing_establishing`, `repeated_framing`, `insufficient_cutaway`,
`no_reaction_coverage`, `impractical_duration`, `identity_risk`,
`unnecessarily_expensive`, `timing_mismatch`, `missing_reference`, `continuity`,
`other`.

Two of these come from **code, not the model** — arithmetic and budget are facts:

- `timing_mismatch` — coverage does not sum to the beat
- `impractical_duration` — a paid shot outside the 3–10s generation range

Warnings are stored on each beat's plan, so they can render beside the shot.

---

## 5. Rules the frontend must respect

**Coverage must sum to `beat_duration` exactly** (±0.05s). `lock` and `compile`
both refuse otherwise. If you build shot-duration editing, re-normalise on the
client or expect a 400.

**`plan.beat_duration` is a snapshot.** If narration is re-recorded, the beat's
real duration moves and the plan becomes stale. Compare `plan.beat_duration`
against the live `beat_duration` in the `scene` response and surface the
divergence — a preview once ran 81 seconds short while every status said green,
because nothing compared what was built against what was current.

**Never write `motion_type: "ai_video"` from the client without going through the
planner or router.** The router is what guarantees a legal duration for the chosen
model. `veo_3_1` accepts only 4/6/8 seconds, so a 3.2-second shot is illegal there
and will fail at generation, not at save.

**Status transitions:**

```
(none) ──plan──> draft ──lock──> locked ──compile──> compiling ──> compiled
                   ▲               │                                  │
                   └──lock=false───┘                                  │
                                     ordinary render with ?force=true ▼
                                                                  orphaned
```

**Job keys are per beat**: `director_plan:<first_beat>`, `director:<beat_id>` for
compile. Two different beats can be worked on independently; the same beat twice
returns 409 rather than a false success.

---

## 6. What is NOT implemented

Deliberately, per scope:

- **Generation from a locked plan.** `compile` exists and works — it is Spike B,
  proven on four beats of HistoryLesson — but it is driven by hand-authored plans
  and is not wired to the planner. Treat it as out of scope for this phase.
- **Library search.** `source`/`source_ref` exist; there is no index or lookup.
- **Identity confidence.** `constrained_by: identity_reliability` is reserved.
  Spike A is blocked on a character having a `structural_anchor`, and none does.
- **FCPXML emitting sub-clips.** `timeline.build()` still lays one clip per beat.
  Sub-clips exist on disk at `render/<beat>/<shot>.mp4`.
- **Automated taste modelling.** The ledger records; nothing learns yet.

---

## 7. Suggested UI shape

Not prescriptive — this is what the data supports.

```
SCENE  s004 – s006          profile [historical_docudrama ▾]    est $3.15
  notes: "less dramatic, more environment"        [ Plan coverage ]

  ⚠ no establishing shot before the interior (scene)
  ⚠ s004.02–04 are all close-ups → widen s004.03

  BEAT s004   26.48s   coverage 26.48s ✓   draft        [ Lock ]
   ├ s004.01  4.0s  ws   establishing   parallax   $0.15
   ├ s004.02  3.5s  cu   insert         static     $0.15
   ├ s004.03  5.0s  mw   master         ai_video   $0.65   ⓘ duration_quantized
   └ …
```

Three things worth surfacing that the data makes cheap:

1. **`coverage_total` vs `beat_duration`** as a running tally — it is the gate on
   locking, and a user editing durations needs to see it move.
2. **`constrained_by`** as a small badge, not buried.
3. **`summary.estimated_cost`** before anything is locked. The whole point of
   plan-only is deciding what to spend before spending it.
