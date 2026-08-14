# vNext architecture gap evaluation

Evaluation of `video_automation` against the vNext Spec, per the Code Evaluation
Scaffold. Read-only: no schema, contract, data or behaviour was changed.

- **Repo state:** `main` @ `ebffeef`, suite 324 passed / 0 skipped
- **Authority:** `vNext Spec` sheet; `docs/FILMCRAFT_V1_CODE_IMPLEMENTATION_CONTRACT.md`
- **Date:** 2026-08-14

Claims below cite `file:symbol`. Where I infer rather than verify, it says so.

---

## 1. Executive assessment

**The migration spine is shorter than the spreadsheet implies.** Four items the
Roadmap lists as P0 are substantially built already, because V1 slices 4 and 5
independently arrived at two of vNext's core invariants:

- **Invariant B — immutable append-only renders.** `backend/generation.py`
  already implements it: `GenerationAttempt` is append-only, retries branch from
  `parent_attempt`, terminal states reject conflicting rewrites
  (`generation.TerminalConflict`), and prior attempts are never destroyed. This
  survived two adversarial audit rounds.
- **Invariant E — timeline non-destructive and separate from generation.**
  `backend/slots.py` is exactly "timeline owns editorial state": slot identity is
  derived and survives take swaps, trims belong to the slot rather than the
  media.
- **Provider capability registry.** `backend/capabilities.py:VIDEO_CAPS` already
  holds per-model `allowed_durations`, `min/max_seconds`, wire types,
  `supports_generate_audio`, `needs_start_image`, `supports_reference_image`,
  `cost_per_second` and a `verified` flag, consumed by
  `capabilities.resolve()`.
- **Invariant F, partially.** Paid generation is gated on an approval signature
  (`director.approval_is_current`), guarded against re-billing
  (`director.paid_signature`), and carries per-attempt cost and provenance.

**The single largest structural gap is Invariant C.** Job state is an in-memory
dict — `pipeline_worker.py:25`, `_jobs: Dict[tuple, Dict[str, Any]]`. Nothing
persists it. A Cloud Run cold start erases every running job, and the frontend
already carries a comment acknowledging that a running job simply vanishes. Every
Pixo/CinemaDrop finding about reconciliation, the global render queue and
origin-aware navigation depends on fixing this first.

**The largest conceptual conflict is the hierarchy.** vNext is
`Project → Scene → Panel → Beat → GenerationAttempt → Active Take → Timeline
Clip`. We have `Project → Shot(narration beat) → DirectorShot → attempt → slot`.
Two mismatches: there is no Scene level, and **"beat" is inverted** — ours is the
narration unit *containing* shots; vNext's is a timed unit *inside* a panel.
Renaming without resolving that will produce silent, expensive confusion.

**Recommendation: evolutionary migration, not rewrite.** The domain modules are
sound and heavily tested. The thing that fights the target model is not the
schema, it is `backend/main.py` at 5,061 lines with ~100 routes, which is where
orchestration, prompt assembly, provider calls and state transitions all meet.

---

## 2. Current architecture

**Stack (verified).** FastAPI backend (`backend/main.py`), Next.js frontend
(`frontend/src/app/page.tsx`, 1,236 lines), per-project JSON state on disk or a
GCS FUSE mount, fal.ai for images and video, ElevenLabs for voice.

**Domain objects and their files:**

| Object | Where | Role |
|---|---|---|
| `Storyboard` | `manifest.py:212` | project root; holds `shots`, render/mix/grade config |
| `Shot` | `manifest.py:165` | **a narration beat**, not a camera shot |
| `CoveragePlan` | `director.py:174` | one beat's coverage; approval + critic warnings |
| `DirectorShot` | `director.py:89` | one visual unit inside a beat |
| `GenerationAttempt` | `generation.py:51` | one immutable attempt to produce media |
| `TimelineSlot` | `slots.py:40` | one editorial position in the cut |

**State on disk, per project:**

```
storyboard_manifest.json     Storyboard + Shots
director/<beat>.json         CoveragePlan (+ approval, warnings, dispositions)
generation/<beat>.json       GenerationAttempt ledger, append-only
timeline_slots.json          TimelineSlot list (trims, media, source attempt)
references.json              reference registry (flat)
characters.json              character anchors
prompt_ledger.jsonl          prompt/ledger events
```

Writes go through `backend/atomic.py` (unique temp, per-destination lock,
bounded replace retry). Project identity is per-request via
`backend/projects.py` `ProjectContext` bound to a `ContextVar`.

**Current flow** (`backend/stages.py:STAGE_ORDER`):
`script → direct → generate → roughcut → refine → export`

**Target flow** (vNext): `Source → Creative DNA → Concepts → Story Lock →
Elements → Director Plan → Storyboard → Motion → Render QC → Shot Review →
Rough Cut → Director Review → Audio/Finish → Delivery`

The V1 spine is a contiguous subset of the target. Creative DNA, Concepts,
Elements, Render QC and Delivery are additions at the two ends, not
replacements in the middle — which is what makes evolutionary migration viable.

---

## 3. Gap matrix

Status: **CURRENT** built and tested · **PARTIAL** exists, incomplete ·
**MISSING** absent · **CONFLICTING** exists but fights the target model.

### P0

| Roadmap item | Status | Evidence |
|---|---|---|
| Validation stamp / generation gate | **PARTIAL** | Approval binds to plan content (`director.plan_signature`, `approval_is_current`) and drift invalidates it; compile refuses draft/drifted/undecided-warning plans (`main.py` compile endpoint). Missing: a *stamp* covering prompt + refs + capability profile, and preflight over that stamp. |
| Scene + character reference system | **PARTIAL** | `characters.py` (anchors, sheets, `lock_sheet`), `references.json` via `main.py:434 _ref_registry`. Missing: locations, props, stable semantic IDs, Visual Style entity. |
| Shot state-change + narrative-function schema | **PARTIAL** | `DirectorShot` has `purpose`, `subject`, `shot_size`, `angle`, `composition`, `reason`, `gestural`, `identity_critical`. Missing: state change, event beats, transition logic, authenticity event. |
| Deterministic post-production derivative graph | **MISSING** | `timeline.py` renders a preview and FCPXML; `bundle.py` zips. No master→derivative graph, no 9:16/1:1 children. |
| Creative DNA + source fidelity | **MISSING** | `Storyboard` has `channel`, `cultural_origin`, `music_prompt`, `narrator_name`. No genre/style/format/era/pacing/fidelity enum. |
| Concept exploration / selection gate | **MISSING** | `script.py` drafts one script. No concept candidates. |
| Moment + frame geometry in DirectorShot | **MISSING** | `composition` is free prose; no moment/keyframe field, no fg/mg/bg screen-position model. |
| Generation state reconciliation | **CONFLICTING** | `pipeline_worker.py:25` `_jobs` is in-memory; `get_jobs_status()` reads it; a cold start loses everything. Needs replacement, not extension. |
| Prompt truncation preflight | **MISSING** | `assets.py:651 soften_prompt` rewrites prompts on rejection but nothing checks length before submit or protects required content. |
| Guided storyboard → motion → rough-cut handoff | **PARTIAL** | `stages.py` computes stage status, blocking reasons and one CTA per stage server-side. Missing the finer target sequence (Render QC, Shot Review, Director Review). |
| Panel → Beat → GenerationAttempt schema | **CONFLICTING** | `Shot`(beat) → `DirectorShot` → `GenerationAttempt` is the same *shape* with inverted naming and no Scene level. Highest-risk rename in the migration. |
| Immutable generation history + active take | **CURRENT** | `generation.py` append-only; `DirectorShot.selected_attempt` references the chosen one. Gap: the selector lives on the shot, vNext wants `panel.active_generation_id`. Naming, not mechanism. |
| Global render queue / origin-aware navigation | **MISSING** | Depends on job authority. `get_jobs_status()` is project-scoped but not durable and stores no origin. |
| Provider capability registry | **CURRENT** | `capabilities.py:VIDEO_CAPS` + `resolve()` + `estimate_shot_cost()`. Gap: images are not covered, and `assets.py` calls `fal_client.subscribe` directly at ~10 sites. |
| Automated post-render QC | **MISSING** | `planner.critique` reviews *plans*; nothing reviews rendered media. |
| Code architecture gap evaluation | **CURRENT** | This document. |

### P1

| Roadmap item | Status | Evidence |
|---|---|---|
| Semantic reference manifest + prompt binding | **PARTIAL** | `assets.py:607 _compose_prompt` takes character anchors; binding is by name, not verified ID. No manifest validator. |
| Adversarial audit integration | **CURRENT** | `docs/audits/`, charter, `orchestration_guardrails.md`; six rounds run. |
| Contextual "What's next" | **PARTIAL** | `stages.py` returns one CTA per stage. Not artifact-aware action objects. |
| Protected prompt compression | **MISSING** | `soften_prompt` has no protected classes. |
| Separate image/video prompt compilers | **PARTIAL** | Distinct paths exist (`assets._compose_prompt` for stills; `director.generate_paid_clip` for motion) but neither is a compiler over a canonical shot spec, and both embed provider specifics. |
| Automatic model/settings recommendation | **PARTIAL** | `capabilities.resolve()` picks a model from duration + gestural intent and returns a reason. No cost-aware recommendation surfaced for override. |
| Revision diff / scoped rerender | **MISSING** | `paid_signature` detects that inputs changed but there is no spec diff and no smallest-render-unit scoping. |
| Timeline specialist service | **PARTIAL** | `slots.py` owns editorial state cleanly; it is a module, not a service, and `timeline.py` still renders from `sb.shots`. |
| Preview/export parity | **MISSING** | `timeline.build_preview` and the FCPXML path both derive from the manifest but nothing asserts they agree. This is also V1 slice 7's §11.7. |
| Quick Generate mode | **PARTIAL** | One-off endpoints exist (`main.py:3344` image, `main.py:3580` generate_video, `main.py:3031` render/reference) but they mutate canonical project state rather than standing outside it. |

---

## 4. Retain / Adapt / Replace

| Module | Call | Reason | Migration risk |
|---|---|---|---|
| `generation.py` | **Retain** | Already Invariant B, audited twice | Low — add `initiated_by`, origin |
| `slots.py` | **Retain** | Already Invariant E | Low |
| `atomic.py` | **Retain** | Durable writes, learned from three audit rounds | None |
| `projects.py` | **Retain** | Per-request identity; vNext assumes it | None |
| `capabilities.py` | **Retain + extend** | Registry shape is right; extend to images and refs | Low |
| `stages.py` | **Adapt** | Server-derived stage model is the right pattern; the stage list grows | Low |
| `ledger.py` | **Adapt** | Cost/provenance exists but predates `GenerationAttempt`; two records of the same truth | Medium — decide one owner |
| `director.py` | **Adapt** | `DirectorShot` becomes Panel or Beat; approval logic survives | **High** — 1,379 lines, the hierarchy rename lands here |
| `manifest.py` | **Adapt** | `Shot` must yield the "beat" name and gain a Scene parent | **High** — every module reads it |
| `timeline.py` | **Adapt** | Must render from slots, not `sb.shots` | Medium |
| `characters.py` + `_ref_registry` | **Replace** | Two half-registries; vNext needs one entity registry with stable IDs | Medium |
| `pipeline_worker.py` | **Replace** | In-memory jobs cannot satisfy Invariant C | Medium — small module, wide blast radius |
| `assets.py` prompt paths | **Replace** | Prompt assembly is interleaved with provider calls; a compiler cannot be extracted incrementally | **High** — 1,145 lines, ~10 direct `fal_client` sites |
| `main.py` | **Decompose** | 5,061 lines, ~100 routes; the real obstacle | **High** — but decomposable route-group by route-group |

---

## 5. Schema deltas

Proposed, not migrated. Existing fields marked ✅.

**Entity** (new — Invariant D)
```
id: str                 # stable semantic, e.g. "char.vesper", "loc.rice_terrace"
kind: character | location | prop | visual_style
name, description
reference_assets: [str]
canonical_asset: str
version: int
provenance: {created_by, created_at, source}
```
Replaces `characters.json` + `references.json`. `visual_style` as an entity is
what makes Creative DNA propagate instead of being re-prosed per shot.

**Panel** (from `DirectorShot`)
```
id ✅  scene_id (new)  beat_id ✅(renamed: parent narration unit)
purpose ✅ subject ✅ shot_size ✅ angle ✅ composition ✅
moment: str            # new — the exact instant to photograph
frame_geometry: {foreground, midground, background, screen_positions}  # new
narrative_function, state_change, transition  # new
entity_refs: [str]     # new — replaces reference_dependencies (name-based)
beats: [Beat]          # new — timed units inside the panel
active_generation_id   # rename of selected_attempt ✅
```

**Beat** (new — vNext sense)
```
index, start_s, end_s, action, camera_behavior, dialogue_vo
```
Must cover the panel duration without overlap. **Note the name collision: our
current `Shot.scene_id` "beat" is vNext's parent narration unit, not this.**

**GenerationAttempt** — mostly ✅
```
id ✅ shot_id ✅ beat_id ✅ attempt ✅ parent_attempt ✅ status ✅
kind ✅ backend ✅ paid ✅ cost ✅ signature ✅ idempotency_key ✅
output ✅ error ✅ started_at ✅ finished_at ✅
initiated_by: user | director | system | recovery   # new
origin: {project, scene, panel, workspace}          # new
provider_job_id, resolved_model, parameters         # new
qc_findings: [QCFinding]                            # new
```

**Job** (replaces the in-memory dict)
```
id, project_id ✅, kind, state: QUEUED|RUNNING|READY|FAILED
origin: {scene, panel, attempt}
created_at, updated_at, heartbeat_at
log (bounded) ✅, error
```
Must be on disk, reconciled on reconnect, and survive a cold start.

**ProviderCapability** — extend `VIDEO_CAPS` ✅
```
+ modality: image | video
+ max_prompt_chars/tokens
+ max_reference_images
+ supports_audio ✅ (supports_generate_audio)
+ price model ✅ (cost_per_second)
```

**QCFinding** (new — Invariant G)
```
id, attempt_id, category: identity|motion|physics|artifact|prompt_adherence
severity, confidence, timestamp_s | frame
detail, disposition: auto_accept | needs_review | rejected
```
**This is the same object as V1 slice 6's Refine issue.** Build once.

---

## 6. Migration phases

Eight bounded phases, ordered by what unlocks what.

**Phase 1 — Job authority.** Replace `pipeline_worker._jobs` with durable job
records; add origin; reconcile on reconnect. *Unlocks: global render queue,
origin-aware navigation, reconciliation.* Independently testable, reversible.
Do first — it is the largest gap and blocks three P0s.

**Phase 2 — Entity registry.** One registry with stable semantic IDs for
characters, locations, props, Visual Style; migrate `characters.json` and
`references.json` on read. *Unlocks: semantic binding, Creative DNA propagation,
scene refs.*

**Phase 3 — Canonical shot spec + prompt compilers.** Extract prompt assembly
out of `assets.py` into image and motion compilers over one shot spec; add
truncation preflight and protected content classes; extend the capability
registry to images. *Unlocks: model-agnostic routing, validation stamp.*
**Highest paid-generation risk — audit before merge.**

**Phase 4 — Validation stamp + generation gate.** Stamp covering spec, prompt,
resolved refs and capability profile; invalidate on any change; hard-block
generation on an unstamped or stale package. Extends the existing approval
signature rather than replacing it.

**Phase 5 — Render QC.** `QCFinding` on attempts; post-generation review;
configurable auto-accept threshold. **Design jointly with V1 slice 6's issue
model.**

**Phase 6 — Hierarchy rename (Scene / Panel / Beat).** Deliberately late: it
touches every module and delivers no user-visible capability. Doing it after
1–5 means renaming a settled model rather than a moving one.

**Phase 7 — Creative DNA + Concepts.** Additive at the front of the pipeline;
`creative_dna.json`, concept candidates, selection gate. Low coupling.

**Phase 8 — Delivery derivatives + Quick Generate.** Master → derivative graph
(9:16, 1:1, grades, captions); isolate one-off generation from canonical state.

---

## 7. Risk register

**Paid generation.** Phases 3 and 4 touch the path that spends money. Six audit
rounds found four Criticals on this path already, all in code that looked
correct. Every change here needs adversarial review and mutation-sensitive
tests before merge.

**Data migration.** Live projects hold manifests, coverage plans, generation
ledgers and slot files. The approved constraint stands: migrate on read,
idempotent, derive only what is safely derivable, never fabricate provenance.
Phase 6's rename is the dangerous one — plan files are keyed by beat id on disk.

**Timeline/export regression.** V1 slice 7 (frozen export, §11.7 equivalence) is
unbuilt. Adding derivatives in phase 8 before that lands would multiply the
surface where a deliverable can disagree with what was reviewed.

**Provider coupling.** `fal_client.subscribe` appears at ~10 sites in
`assets.py`, interleaved with prompt building and retry/softening logic. This is
the main obstacle to model-agnostic routing and cannot be extracted in small
pieces.

**`main.py` at 5,061 lines.** Every phase adds routes. Without decomposition it
becomes the bottleneck for parallel work and the place merge conflicts land.

**Double-build risk.** V1 slice 6's issue model and vNext's QCFinding are the
same object. Building slice 6 to the V1 contract first means rebuilding it.

---

## 8. Test strategy

**Existing (324 tests, all passing):** approval/plan signature
(`test_plan_approval.py`), generation lineage and no-double-spend
(`test_generation_lineage.py`), project isolation (`test_project_isolation.py`),
timeline slots (`test_timeline_slots.py`), stages (`test_stages.py`), Gate 1 and
manifest load/save (`test_manifest.py`, pending merge), paid re-bill
(`test_paid_rebill.py`).

**Required before each phase:**

- **P1 jobs** — a job survives process restart; reconnect reconciles a completed
  job into its origin; a lost job is reported, never silently dropped.
- **P2 entities** — an ID resolves to the same asset across projects; a prompt
  with an unresolved ID hard-fails rather than rendering the name as prose.
- **P3 compilers** — a prompt that would be truncated blocks generation;
  protected classes survive compression; the same shot spec compiles to image
  and motion packages without divergent intent.
- **P4 stamp** — any change to spec, prompt, refs or model invalidates the
  stamp; generation with a stale stamp is refused before dispatch.
- **P5 QC** — findings carry category/severity/confidence/timestamp; a blocking
  finding prevents auto-accept into the active take.
- **P6 rename** — every existing project loads, and every plan/ledger/slot file
  is found under the new names, on read, idempotently.

Standing rule from `docs/audits/orchestration_guardrails.md`: a fix does not
count until a faithful mutation of it fails a test.

---

## 9. Open questions

Only what inspection cannot answer.

1. **Scene boundaries.** vNext adds a Scene level above Panel. Today
   `Storyboard.shots` is flat. Are scenes authored by the writer, derived from
   the script by the Director, or both?
2. **Panel vs our beat.** Is a vNext Panel the same granularity as our
   `DirectorShot` (one visual unit), or coarser — a narration beat that contains
   several? The whole rename depends on this, and both readings fit the spec.
3. **Ledger ownership.** `ledger.py` and `generation.py` both record cost and
   provenance. Which is authoritative in vNext, or does `ledger.py` become a
   projection?
4. **QC vision model and budget.** Automated render QC implies a vision model
   per generated clip. Which provider, and what per-clip cost is acceptable
   given the $15–25 per finished video target in CLAUDE.md?
5. **Concept images.** Concept exploration generates 3–6 candidates with images
   before any approval gate. What spend cap applies to a stage that runs before
   the budget gate exists?
6. **V1 completion.** Does V1 ship (slices 5b–8, deploy, §15 walkthrough) before
   phase 1, or do the two run concurrently? This determines whether slice 6 is
   built to the V1 contract or to vNext's QCFinding.

---

## Recommendation

Run **phase 1 (job authority)** first: it is the largest verified gap, it blocks
three P0s, and it is independently testable and reversible.

Hold **V1 slice 6** until phase 5's QCFinding schema is agreed, so the issue
model is built once.

Route this evaluation and the phase-1 plan through adversarial review before
implementation, per the scaffold's own AO expectation.
