# FilmCraft V1 — Implementation Contract for Claude Code

## Role and authority

You are the primary implementation owner for FilmCraft V1.

Your job is to implement the approved FilmCraft product behavior in the existing `video_automation` repository without redesigning the product.

Authority order:

1. This implementation contract
2. Approved Figma V1 UX
3. Existing backend/API contracts where they do not conflict with the approved product behavior
4. Existing implementation details

If the existing code conflicts with this contract, do not silently preserve the old behavior. Surface the conflict and implement the smallest safe refactor needed to satisfy the approved product model.

Do not invent new workflow stages, hidden modes, alternate approval systems, or backend-facing UI concepts unless required to make the approved behavior function.

---

# 1. Product goal

Primary success criterion:

> I have a script. Get me to a watchable first cut.

Persistent product spine:

**SCRIPT → DIRECT → GENERATE → ROUGH CUT → REFINE → EXPORT**

The film should remain the persistent object throughout the experience.

The product must not require:
- curl
- hidden prerequisites
- manual pipeline steps
- node wiring by the user
- backtracking to disconnected workspaces
- knowledge of backend model routing or storage internals

The UI should feel like filmmaking first and software second.

---

# 2. Global workflow rules

## 2.1 Persistent stage navigation

All major creation screens must preserve the six-stage spine:

1. Script
2. Direct
3. Generate
4. Rough Cut
5. Refine
6. Export

The user must always be able to tell:
- current stage
- completed stages
- blocked stages
- next meaningful action

Primary CTA progression:

- Script: **Continue to Direct**
- Direct: **Approve Beat Coverage** / scene-level approval where appropriate
- Generate: **Build Draft 1**
- Rough Cut: **Continue to Refine**
- Refine: **Continue to Export**
- Export: **Export Final Master**

Do not create alternate competing primary flows.

## 2.2 Stage ownership

Each stage has one authoritative responsibility.

### Script owns
- script text
- scene boundaries
- narration text
- narration timing / timing spine
- narrator/voice selection state

### Director owns
- visual intent
- scene strategy
- beat coverage
- sub-shot definitions
- shot type
- intended duration
- rationale
- Critic review state
- approval state

### Generate owns
- references used for generation
- model execution
- generation attempts
- retries
- branches
- takes
- selected generated/treatment output
- paid generation spend

### Rough Cut owns
- chronological edit
- clip placement
- timeline trims
- timing relationships
- VO/music/SFX placement
- watchable Draft 1

### Refine owns
- targeted fixes
- issue review
- non-destructive polish
- audio/grade refinements
- routing the user to the authoritative source stage when required

### Export owns
- final validation
- version naming
- frozen export snapshot
- final master
- FCPXML
- export history

No stage may silently mutate another stage's authoritative decisions.

---

# 3. Core data model invariant: beat != shot

A narration beat is a **visual time budget**, not a single shot.

Example:

A 28-second narration beat may be covered by:

- 3A — 0–6s — motion wide establish
- 3B — 6–11s — still/parallax detail
- 3C — 11–18s — motion worker medium
- 3D — 18–23s — still insert
- 3E — 23–28s — motion dramatic close

The Director planner must support multiple visual units per beat.

Do not collapse this into one beat = one shot.

Required relationship:

`Narration Beat -> one or more DirectorShots`

Every DirectorShot must remain traceable back to its:
- project
- scene
- beat
- intended duration
- approved intent

---

# 4. Script stage behavior

The Script stage prepares the timing spine.

The UX must teach:

> Director covers each narration beat with one or more visual units sized to its timing.

Longer beats may become several:
- motion shots
- stills
- parallax shots

The stage must not imply one beat = one shot.

Voice labels must be data-driven. Do not hard-code `Vesper` into the UI.

Use the active narrator/voice name if one exists; otherwise use neutral language such as:
- Narrator
- Draft voice
- Voice track

Script changes after downstream work exists must not silently corrupt the plan or cut.

When a script or timing change invalidates downstream work:
- mark affected scenes/beats stale
- preserve existing generated media and edit history where safely possible
- require re-plan where intent/timing changed materially
- do not silently remap unrelated assets

---

# 5. Director stage behavior

Director is a planning and critique stage.

Canonical flow:

**SCRIPT/NARRATION → DIRECTOR PLAN → CRITIC REVIEW → HUMAN APPROVAL → GENERATE**

No paid generation may occur before explicit approval.

## 5.1 Director plan

For each beat, Director may propose multiple visual units.

Each visual unit should include:
- shot/sub-shot ID
- source beat ID
- type: motion / still / parallax
- planned duration
- framing
- camera behavior
- rationale
- reference requirement if applicable
- estimated generation cost if paid work is expected

## 5.2 Critic

Critic is a separate review pass after planning.

Critic must challenge the plan rather than merely echo it.

Examples:
- repeated composition
- protagonist overuse
- missing coverage
- weak escalation
- unnecessary paid motion
- rhythm problems

Critic issues must attach to:
- a specific shot, or
- a specific beat gap / beat ending

Actions may include:
- Accept fix
- Ask Director to revise
- Keep as-is

Critic should not be implemented as a disconnected giant workspace.

## 5.3 Director lineage boundary

Director must stop before generated takes.

Correct Director lineage:

**Director Intent → Reference Requirement / Approved Reference → Generation Plan**

Example final node:

**Motion planned • 7 sec • auto route • est. $0.42 • no paid generation yet**

Incorrect Director lineage:

**Director Intent → Take A / Take B / Take C / Selected**

Takes belong to Generate.

## 5.4 Approval

Approval must be explicit before paid generation.

Beat-level approval must be supported.

To avoid excessive clicks, the implementation may also support higher-level actions such as:
- Approve Remaining Clean Coverage
- Approve Scene Plan

But unresolved Critic warnings must not be silently approved by bulk actions.

Approval state must be durable and attributable to the exact plan version.

If the plan materially changes after approval:
- invalidate approval for affected units
- preserve old plan/history
- require re-approval before new paid generation

---

# 6. Generate stage behavior

Generate is the primary **lineage workspace**.

This is where approved Director intent becomes real media.

The user must not manually wire nodes.

Relationships are automatic.

Canonical lineage:

**Approved Shot → Reference → Generate/Treat → Output Branches → Selected Output**

Examples:

### Motion
`Shot 3A -> Reference -> Motion Generate -> Take A / Take B / Take C -> Selected`

### Still/parallax
`Shot 3B -> Approved Still -> Parallax Treatment -> Selected Output`

### Retry
`Shot 3C -> Reference -> Attempt 1 Failed -> Retry Branch -> Attempt 2 -> Selected`

A failed generation must remain attached to the intended shot.

Retries must branch from the same approved shot.

Retries must never silently overwrite or erase prior attempts.

Changing the shot's creative intent, type, or planned duration is not a retry. That returns to Director.

## 6.1 Spend

Generation spend must be explicit.

The UI/state should be able to show:
- spent so far
- estimated remaining
- attempt-level cost where available
- retry estimate

No retry may double-charge because of duplicate requests, UI retries, job retries, or network retries.

Idempotency must be treated as a core product requirement.

## 6.2 Incomplete generation and Draft 1

The user may build a Rough Cut before all generation completes.

If coverage is incomplete, make it explicit.

Example:

**3/5 visuals ready • Draft 1 will use 2 placeholders**

Primary action:

**Build Draft 1**

Placeholders must preserve:
- source shot ID
- intended duration
- timeline slot
- expected media type

When a missing selected output later becomes available, it must be able to replace the placeholder without destroying the edit.

Do not silently substitute unrelated media.

---

# 7. Rough Cut behavior

Rough Cut is the transition from lineage-first thinking to time-first editing.

The film now exists.

Primary layout:
- cinema player
- chronological timeline
- VIDEO track
- VO track
- MUSIC track
- SFX track
- contextual inspector

The timeline is authoritative for edit timing.

## 7.1 Timeline slot invariant

A timeline visual clip is fundamentally a **slot tied to a DirectorShot**, not merely a file path.

Example:

`Timeline Slot -> Shot 3C -> Selected Output Take B`

If the user selects Take D later, Take D should replace media in the existing Shot 3C slot without destroying:
- slot identity
- trims where valid
- surrounding edit relationships
- scene placement

Changing selected media is different from changing Director intent.

If Director changes the shot's planned duration materially, the timeline may require reconciliation.

Do not silently stretch or retime the cut in a way that hides plan changes.

## 7.2 Source navigation

From a timeline clip, the user must be able to navigate back to its source lineage.

The product should preserve context:
- selected project
- scene
- beat
- shot
- timeline position where possible

---

# 8. Refine behavior

Refine is problem-solving, not another full editing application.

The same film and timeline foundation should remain visible.

Issues should attach to:
- exact shot
- timeline position
- audio event
- scene
- grade region where applicable

Issue categories may include:
- visual
- audio
- rhythm/timing
- continuity
- grade
- missing media

Each issue should have:
- severity
- blocking vs non-blocking
- diagnosis
- suggested smallest fix
- authoritative stage responsible for the fix

Routing rules:

- change media/take -> Generate
- change timing/edit -> Rough Cut or Refine
- change shot intent/type/planned duration -> Director

No screen should quietly mutate another stage's authoritative state.

Optional polish must not block export.

Blocking issues may block export.

---

# 9. Export behavior

Export is a confidence and delivery stage.

It is not another editing workspace.

The user should see:
- exact final cut being exported
- validation state
- blocking issue count
- delivery preset
- version name
- export history

## 9.1 Frozen snapshot

Every export must bind to an immutable export snapshot representing the exact reviewed cut.

Example:

`FilmCraft Master v1`

The snapshot should identify the exact:
- project version
- script/timing state
- Director plan version
- approved shot state
- selected outputs
- timeline state
- audio state
- grade state

Later edits create a new export version.

Do not overwrite a prior final master.

## 9.2 Deliverables

At minimum V1 must preserve:

### Final Master
Rendered video output.

### FCPXML
Editable timeline export for continued finishing in Final Cut Pro.

Both outputs must be derived from the **same frozen timeline snapshot**.

Critical invariant:

> The rendered master and FCPXML must describe the same approved timeline state.

Do not generate FCPXML from a different in-memory, stale, or partially refreshed timeline representation.

Export history should retain:
- version
- type
- preset
- timestamp
- status
- snapshot/version identifier

---

# 10. Cross-stage invalidation rules

Changes must invalidate only the minimum required downstream state.

Examples:

## Script text changes without timing change
May require Director review if meaning changed materially.

## Narration timing changes
Invalidate affected Director timing and downstream timeline mapping.

## Director changes framing only
May invalidate generation for that shot, but not unrelated shots.

## Director changes duration
May require:
- generation reconciliation
- timeline slot reconciliation
- downstream export invalidation

## Generate selects a different take
Should update the same timeline slot, not recreate the edit.

## Rough Cut trim changes
Must not mutate Director planned intent.

## Export
Creates immutable snapshot and does not mutate prior versions.

---

# 11. Required safety / correctness properties

Treat these as hard requirements.

## 11.1 No double-spend
Retries, network retries, worker retries, duplicate clicks, and job restarts must not cause duplicate paid generations.

## 11.2 No silent substitution
A failed/missing Shot 3C must not silently become Shot 3B media or some generic fallback.

Use an explicit placeholder when required.

## 11.3 No stale project targeting
Switching projects/episodes must not allow queued work or delayed responses to mutate the wrong project.

## 11.4 No false success
The UI must not claim:
- approved
- generated
- saved
- exported
unless authoritative backend state agrees.

## 11.5 No approval drift
An approved plan version must not silently mutate after approval.

## 11.6 No lineage loss
Retries and regenerations must retain prior attempts and preserve ancestry.

## 11.7 Render/FCPXML equivalence
Final master and FCPXML must reflect the same frozen timeline state.

---

# 12. Implementation strategy

Prefer vertical slices over a frontend-only rewrite.

Suggested implementation sequence:

1. Shared stage/status model
2. Script -> Director transition and invalidation
3. Director multi-shot beat model + Critic + approval
4. Generate lineage + retries + idempotency + placeholders
5. Rough Cut slot-based timeline
6. Refine routing and issue model
7. Frozen export snapshot + master + FCPXML
8. End-to-end hardening

For each slice:
- implement backend/state changes
- implement API contract
- implement frontend behavior
- add tests for invariants
- preserve compatibility only where it does not violate this contract

---

# 13. Testing expectations

Tests should prove behavior, not merely implementation details.

Required categories:

- one beat -> multiple DirectorShots
- plan approval versioning
- approval invalidation
- Critic issue attachment
- no paid generation before approval
- generation idempotency
- retry lineage preservation
- failed generation -> placeholder
- placeholder -> later selected output replacement
- take swap preserves timeline slot
- project switch isolation
- stale response rejection
- export snapshot immutability
- FCPXML/master timeline equivalence
- mutation-sensitive safeguards

Do not rely only on mocked happy-path unit tests.

Add behavioral/integration coverage where practical.

---

# 14. Non-goals for V1

Do not turn FilmCraft into:
- Premiere Pro
- Resolve
- ComfyUI
- a generic node editor
- a model-routing dashboard
- an infrastructure console

Do not expose backend implementation concepts unless the user truly needs them.

Do not add more primary workflow stages.

---

# 15. Definition of done

V1 is done when a user can:

1. open or create a script
2. confirm narration timing
3. enter Director
4. review multi-shot coverage for beats
5. review Critic feedback
6. approve coverage before spend
7. generate approved media with visible lineage
8. survive failures/retries without losing intent or double-spending
9. build Draft 1 with placeholders if needed
10. watch and edit the Rough Cut
11. refine targeted issues
12. export an immutable final master
13. export matching FCPXML
14. reproduce which approved state produced each deliverable

The implementation should make the approved UX feel inevitable, not bolted onto the old interface.
