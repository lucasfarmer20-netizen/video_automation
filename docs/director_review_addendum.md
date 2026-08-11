# Director API — Review Surface Addendum

**Companion to `DIRECTOR_API_CONTRACT.md`.** Written 2026-08-10 against `main`.
Everything described here is implemented and pushed, not proposed.

Response to the 158-shot review UX proposal. The 90/10 triage is the right idea
and is now backend-computed. Three things in the proposal need changing, and one
of them is structural.

---

## 1. STRUCTURAL: there are two review surfaces, not one

The proposal's Cinema Scrubber and Montage Matrix both assume shot thumbnails
exist. **At plan time they do not.** A plan is text — no image has been generated,
which is the entire point of plan-only review.

```jsonc
// a shot, straight from GET /api/director/scene, before generation
{ "id": "s012.02", "purpose": "master", "subject": "Single jack striking drill steel",
  "shot_size": "m", "angle": "front", "composition": "…",
  "camera": { "duration": 4.0, "move": "static" },
  "reason": "The blow is the beat's only real action.",
  "clip": "", "draft_variations": [], "chosen_variation": null }   // ← all empty
```

So the review splits in two, and they answer different questions:

| | **Plan review** | **Take review** |
|---|---|---|
| When | before any generation | after compile |
| Cost to reach it | ~$1 (LLM only) | ~$25 (stills + video) |
| Material | framing, purpose, duration, reason | actual images and clips |
| Question | *is this the right coverage?* | *is this a good execution?* |
| Undo cost | free — re-plan | re-generate, real money |

**Cinema Scrubber and Montage Matrix are excellent designs for take review.**
Build them there.

**Plan review needs something else** — closer to a beat sheet than a storyboard.
Shot size, purpose, duration and reason in narration order, so the *rhythm* is
visible: `ws → cu → mw → cu → mw` reads as a shape, and "three close-ups in a
row" is legible without a single picture. It is also the cheaper surface to get
right, because it is the one that prevents spend rather than judging it.

---

## 2. Tier 1 is not $0. It is the largest cost in the film

Measured on a real three-beat scene:

```
17 free-tier shots  →  $3.30      ← the proposal calls these "$0 cost"
 2 paid-tier shots  →  $1.47
```

Parallax and static are free to **move**. Every one still needs a **still at
$0.15**. Across ~158 shots that is roughly **$24 — more than all the AI video
combined.**

A UI that labels 135 shots *"$0 cost, zero clicks"* hides the majority of the
spend behind the reassuring colour. Show tier 1's cost as prominently as tier 3's.
The lock button should read the real total, which `triage()` supplies per tier.

---

## 3. Never write `motion_type` from the client

The proposed shortcut `3 = Set active shot to AI Video` writes an unroutable shot.

Generated video comes in fixed lengths that differ per model, and the wire format
differs too — `wan_2_7` takes an integer, `veo_3_1` takes `"4s"`, `luma` offers
only `5s` or `9s`. A 3.34-second shot set to `ai_video` is not slightly wrong, it
is **unproducible**, and that exact case cost a paid generation today.

Route the keystroke through the backend so it can answer "no legal model for this
duration", snap the shot to a producible length, and rebalance its siblings so the
beat still totals correctly. Same for `1` and `2` — free tiers are safe, but keep
one path so the rules live in one place.

---

## 4. What is implemented

### Tiers — computed in the backend, never in the client

`GET /api/director/scene?beats=…` now returns `tier` and `reason` on every shot,
plus a `triage` block per beat:

```jsonc
"triage": {
  "tiers": {
    "1": { "shots": 14, "cost": 2.10, "ids": ["s011.01", …] },
    "2": { "shots": 3,  "cost": 1.62, "ids": ["s012.02", …] },
    "3": { "shots": 2,  "cost": 1.05, "ids": ["s011.03", …] }
  },
  "needs_review": ["s012.02", "s011.03", …]      // tiers 2 + 3
}
```

The rule, from `director.shot_tier`:

| Tier | Meaning | Triggered by |
|---|---|---|
| **3 — creative** | needs taste, not approval | `identity_critical`, or `face_visibility` ≥ moderate, or a critic warning naming this shot |
| **2 — check** | spends money, or a limit changed the shot | `constrained_by` non-empty, or `motion_type == "ai_video"` |
| **1 — standard** | free, unremarkable | everything else |

Each shot carries a human-readable `reason` alongside the number, because a tier
a reviewer cannot interrogate is one they learn to click past.

**Do not reimplement this client-side.** Two registries have already been
duplicated into this frontend and quietly disagreed with the backend; the
`kling_2_5_turbo_pro` dropdown entry rendered on a different model for months.

### `?tier=needs_review`

Filters `coverage` to tiers 2 and 3 only. Drives the Problem Queue directly:

```
GET /api/director/scene?beats=s011,s012,s013&tier=needs_review
```

`triage` still reports all three tiers, so a header can say "5 of 19 need you"
while the list holds only the five.

### `POST /api/director/lock_scene`  `{"beats":[…]}`

Validates every beat first and **locks none if any fails**. A half-locked scene is
worse than an unlocked one: the render path skips locked beats, so a partial lock
silently produces an episode where some beats have coverage and others do not.
Returns the locked ids and the scene's estimated cost.

### Not built: `GET /api/director/montage`

Deliberately. Pre-generation there is nothing to thumbnail, and post-generation
158 thumbnails come off a GCS FUSE mount — the &lt;50 ms target will not hold. When
take review is real it needs a cached sidecar, the way `/api/audio/peaks` already
caches waveform envelopes. Worth building then, against a known shape.


---

## 6. Three gaps in the current workspace

Written after reading `DirectorWorkspace.tsx` and `directorApi.ts` at `09dea91`.
The rewire to the shipped contract is done and correct — these are what remain.

### 6.1 The survey should be the entry point, not the beat card

`GET /api/director/survey` (new) answers the question that comes *before* opening
a workspace: **which beats are worth covering at all.**

```jsonc
{
  "episode_seconds": 616.0,
  "frozen_if_nothing_covered": 197.0,      // seconds
  "frozen_pct": 32,
  "recommended": ["s001","s006","s011", …],           // scored 3
  "scenes": [["s001"],["s006"],["s011","s012","s013","s014","s015"], …],
  "beats": [
    { "beat_id": "s001", "seconds": 28.2, "motion_type": "ai_video",
      "frozen_if_left": 18.2, "recommend": 3,
      "reason": "18s of this paid beat would be a frozen frame (65% of it)",
      "narration": "…" }
  ]
}
```

Today the only way in is a `DIRECT` button on a BeatCard, which means committing
to cover a beat *before* anything says whether it is worth covering. The survey
is free, instant and deterministic — no LLM call — so it can render on load.

`scenes` is already grouped into contiguous runs, because planning is
scene-level. That list is the work queue: on MichaelHeney it turns 13 recommended
beats into 7 planning calls.

Suggested shape — a list, not a grid:

```
COVERAGE SURVEY                    616s episode · 197s frozen if untouched (32%)

●●●  s001   28.2s  ai_video   18s would be frozen (65%)          [ plan scene ]
●●●  s006   25.9s  ai_video   16s would be frozen (61%)          [ plan scene ]
●●●  s011–s015   129s  ai_video ×5   84s would be frozen         [ plan scene ]
●●   s003   28.2s  parallax   28s on one image (~7 shots' worth)  [ plan ]
○    s018   11.4s  static     short enough to hold                —
```

### 6.2 Warnings belong on the shot they name, not only in a drawer

Every warning already carries `beat_id` and `shot_id` precisely so it can be
rendered in place:

```jsonc
{ "beat_id": "s004", "shot_id": "s004.03", "kind": "repeated_framing",
  "detail": "s004.02 and s004.03 are the same subject from the same axis…",
  "suggestion": "Change s004.03 to a low or three_quarter angle." }
```

A drawer makes the reviewer hold "s004.03 has a problem" in their head while
they go and find s004.03. Put the warning on the card — the shot and its
criticism read together, and the suggestion is actionable where the edit
controls already are.

Keep the drawer as the *queue* (Problem Queue, `?tier=needs_review`), but it
should navigate to shots rather than being the only place the text appears.
A warning with `shot_id: ""` is scene-level and belongs in the header.

### 6.3 Re-critique after edits — and mark the warnings stale meanwhile

`critiqueCoverage()` exists in `directorApi.ts` and nothing calls it. The critic
currently runs only inside **Redirect Scene**, which re-plans — so after a hand
edit the Problems drawer describes the plan you *had*, not the one you have.

Two changes:

* A **Re-check** button calling `POST /api/director/critique` with the scene's
  beats. One LLM call, no re-planning, and the shots you edited are preserved.
* **Invalidate on edit.** Any successful `POST /api/director/shot/{id}` should
  grey the warnings and label them stale until re-checked. Silently stale
  criticism is worse than none, because it reads as current.

`POST /api/director/shot/{id}` (new) is the sanctioned edit path and returns
`coverage_total`, `beat_duration`, `problems` and `notes` on every call — enough
to drive both the running tally and the staleness flag without a second request.

---

## 5. The division that keeps us in sync

**Backend owns:** what a tier means, what a duration may be, which model serves a
shot, whether a plan can compile, what anything costs.

**Frontend owns:** what the reviewer sees, in what order, and how few gestures it
takes.

The failure mode to avoid is the frontend deriving any backend fact — tier,
legal duration, cost, or whether a plan is valid. Every one of those is a query,
and each has already drifted at least once in this codebase when it was copied.

**Open question for both of us:** plan review is unproven. Nobody has yet reviewed
a 25-beat coverage plan and formed an opinion in a reasonable time. The 20
seconds/shot budget is a target, not a measurement. Worth instrumenting the first
real session — how long, how many revisions, which tier consumed the attention —
because that number decides whether coverage ships or stays an experiment.
