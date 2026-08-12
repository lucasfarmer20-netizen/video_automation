# FilmCraft V1 — implementation plan

Plan only; nothing below is built yet. Written against
`FILMCRAFT_V1_CODE_IMPLEMENTATION_CONTRACT.md` and the code as of `a8003ca`.

The headline: **the contract is closer to this codebase than it reads.** The
hard part of §3 (beat ≠ shot) and most of §11.1 (no double-spend) already exist
and work. The real V1 cost is in four structural conflicts — a process-global
active project, take/lineage ownership, approval versioning, and export
snapshots — plus a stage-navigation rewrite. Everything else is assembly.

---

## 1. What already satisfies the contract — reuse, do not rebuild

| Contract clause | Already implemented | Where |
|---|---|---|
| §3 beat → many DirectorShots | `CoveragePlan.coverage: list[DirectorShot]`, IDs are `s003.01` style, `beat_duration` snapshotted at plan time | `backend/director.py:88,167` |
| §5.1 visual unit fields | `purpose`, `shot_size`, `angle`, `composition`, `camera.duration`, `reason`, `reference_dependencies`, `estimated_cost` — a near-exact match to the contract's field list | `director.py:88` |
| §5.2 Critic as a separate pass | `planner.critique()` behind `POST /api/director/critique`, writes `plan.warnings`, read-only otherwise | `main.py:1954` |
| §5.2 issue attachment | `shot_tier()` / `triage()` compute tiers **server-side, once**, with an explicit comment forbidding a client-side copy | `director.py:204,228` |
| §5 no paid generation before approval | `compile_coverage` refuses `ai_video` shots unless `storyboard_approved`; free tiers stay open | `director.py:729` |
| §11.1 no double-spend | `paid_signature()` hashes the inputs a clip was bought for; `paid_clip` is set **only** when fal was billed, deliberately separate from `clip` | `director.py:698` |
| §6.1 spend visibility | `ledger.py` records generation, failure, choice, rejection, plan, plan outcome; `summary()` / `planner_report()` aggregate | `backend/ledger.py` |
| §9.2 FCPXML | `timeline.py` emits OTIO + FCPXML; `bundle.py` zips FCPXML plus every asset it references | `timeline.py`, `bundle.py` |

`paid_signature` is the single most valuable thing in the repo for this contract.
It is exactly the pattern §5.4 and §11.5 ask for (approval bound to a content
signature, invalidated when inputs change) — applied at shot level. Four of the
slices below are that same idea lifted to plan, slot, and export level. Do not
invent a second mechanism.

---

## 2. Conflicts the contract forces — decide these before writing code

The contract says surface conflicts rather than silently preserving old
behavior. These are the five that matter.

### C1. Process-global active project vs §11.3 "no stale project targeting"

**The blocker.** There is one active project per *process*, not per request:
`config.set_active_manifest()` rebinds module globals (`MANIFEST_PATH`,
`ASSETS`, `REFERENCES_DIR`, `REFERENCES_CONFIG`, `CHARACTERS_CONFIG`) at
`config.py:44`, backed by a `.active_project` file at `main.py:144`. Every
endpoint resolves its project by calling `get_current_project()` with no
project argument (`main.py:189`).

Consequences today: switching project mid-job retargets in-flight background
work, because `pipeline_worker` jobs read the same globals. Two browser tabs on
two projects share one global. `tests/conftest.py` exists specifically to undo
this leakage between tests, and its docstring records the order-dependent
failures it caused.

§11.3 cannot be satisfied on top of this. **Recommended fix:** thread an
explicit `project_id` through the API and resolve paths per request, keeping
`get_current_project()` as a deprecated shim during migration. This is the
single largest refactor in V1 and it gates slices 4–7, so do it first.

### C2. Director currently owns takes; §5.3 says Generate owns them

`DirectorShot` carries `draft_variations`, `chosen_variation`, `clip`,
`paid_clip`, `paid_signature`, `error` — produced state living on the planning
object. §5.3 is explicit that Director's lineage must terminate at
"Motion planned • 7 sec • est. $0.42 • no paid generation yet".

**Recommended fix — smallest safe refactor:** do *not* move those fields (too
much of the render path reads them, and `paid_clip`/`paid_signature` are
load-bearing for the re-bill guard). Instead add a separate
`GenerationAttempt` record keyed by `(project_id, shot_id, attempt_n)` that
owns attempts, failures, retries, and cost, and make the Director *view*
project only up to the plan. Director keeps a pointer to the selected attempt;
it stops rendering takes in the UI.

### C3. `CoveragePlan.version` is a schema version, not a content version

§5.4 and §11.5 need approval attributable to an exact plan version and
invalidated on material change. `version: int = PLAN_VERSION` is the
serialization format. `status` is a lifecycle string
(`draft|locked|compiling|compiled|orphaned`), not an identity.

**Recommended fix:** add `plan_signature` (hash over the fields that make the
plan materially different — shot IDs, durations, types, prompts, framing)
alongside `approved_signature` and `approved_at`/`approved_by`. Approval is
valid iff `plan_signature == approved_signature`. Same shape as
`paid_signature`. Materially-changed units lose approval; history is preserved
by never mutating a saved plan in place.

### C4. Export reads live state; §9.1/§11.7 require a frozen snapshot

`GET /api/export/{kind}` (`main.py:4563`) serves whatever `<slug>.fcpxml`
happens to be on disk, and the master renders from the live manifest. Nothing
binds the two to the same state, which is precisely the equivalence §11.7 calls
critical. There is also no export history and no version identity.

**Recommended fix:** an export writes an immutable snapshot directory
(`exports/<version>/`) containing the frozen manifest, plan set, selected
outputs, timeline, audio and grade state. Master **and** FCPXML are both
rendered *from that directory*, never from live state. Later edits create a new
version; prior versions are never overwritten.

### C5. "Build Draft 1" with placeholders vs a preview that refuses to build

§6.2 requires Draft 1 to build with explicit placeholders
("3/5 visuals ready • Draft 1 will use 2 placeholders"). Today the two paths
disagree: FCPXML leaves missing shots as **gaps** and prints a note
(`timeline.py:316`), while the preview builder **raises** and refuses to render
(`timeline.py:447-454`). Neither is a placeholder.

**Recommended fix:** a real placeholder clip generated at the shot's intended
duration, carrying `shot_id` / expected media type / slot identity, so a later
selected output replaces it in place (§7.1) without disturbing trims.

### C6 (minor, but flag it). Hardcoded "Vesper" vs §4 "voice labels must be data-driven"

`Vesper` is hardcoded in ~8 UI locations (`page.tsx`, `BeatCard.tsx`,
`AssemblyPanel.tsx`, plus the `VesperChat` component name). **This is a genuine
tension with CLAUDE.md**, which names Vesper's narration voice as the channel's
deliberate throughline. My read: the contract is about the *product* not
assuming a narrator, while CLAUDE.md is about what *this channel* configures.
Both hold if the name becomes a project setting that defaults to Vesper. Worth
your explicit call, since it is the one place the two authorities disagree.

---

## 3. Stage navigation: 5 steps → 6 stages

`StepHeader.tsx` defines `StepId = 1 | 2 | 3 | 4 | 5` with `buildSteps()`
gating on `scriptLocked` / `storyboardApproved`. The contract's spine is six
named stages with per-stage CTAs and blocked/completed state.

The existing shape is right — a server-derived gate model with reasons for
blocking — it is the *cardinality and naming* that change. Replace numeric
`StepId` with a named union (`script|direct|generate|roughcut|refine|export`),
and derive stage status from backend state rather than the frontend's local
counts, per §11.4 ("no false success" — the UI must not claim a stage complete
unless authoritative backend state agrees).

`page.tsx` is 1236 lines and already holds most cross-stage state. Splitting it
per stage is a prerequisite for slices 5–7, not optional cleanup.

---

## 4. Slice sequence

Vertical slices per §12. Each slice = state + API + frontend + invariant tests.
Slices 0–3 are the foundation; nothing after 3 is safe until C1 lands.

| # | Slice | Delivers | Key files | Contract |
|---|---|---|---|---|
| 0 | **Stage model** | named 6-stage union, server-derived status/blocked/next-action, one `GET /api/stages` | `StepHeader.tsx`, `page.tsx`, `main.py` | §2, §11.4 |
| 1 | **Per-request project identity** | `project_id` threaded through API + worker; globals demoted to a shim; stale-response rejection | `config.py`, `main.py`, `pipeline_worker.py` | §11.3 — **C1** |
| 2 | **Plan signature + approval** | `plan_signature`/`approved_signature`, beat + scene approval, invalidation on material change, history preserved | `director.py`, `main.py` | §5.4, §11.5 — **C3** |
| 3 | **Script → Director invalidation** | timing/text change marks affected beats stale without remapping unrelated assets | `script.py`, `director.py` | §4, §10 |
| 4 | **Generate lineage** | `GenerationAttempt` record; retries branch and never overwrite; failures stay attached; request-level idempotency key on top of `paid_signature` | new `generation.py`, `director.py`, `ledger.py` | §6, §11.1, §11.6 — **C2** |
| 5 | **Slot-based timeline** | timeline clip = slot → DirectorShot → selected output; take swap preserves slot, trims, placement; placeholders | `timeline.py`, `MultitrackTimeline.tsx` | §7.1, §6.2 — **C5** |
| 6 | **Refine routing** | issue model with severity/blocking + authoritative-stage routing; no cross-stage mutation | `ProblemQueueDrawer.tsx`, new `issues.py` | §8 |
| 7 | **Frozen export** | `exports/<version>/` snapshot; master + FCPXML both derived from it; export history | `bundle.py`, `timeline.py`, `main.py` | §9, §11.7 — **C4** |
| 8 | **Hardening** | end-to-end §15 walkthrough, mutation-sensitive tests | `tests/` | §13, §15 |

**Recommended order deviation from the contract's §12 suggestion:** the contract
lists the shared stage/status model first, then Script→Director. I put
per-request project identity at slice 1 instead, ahead of everything else,
because C1 makes §11.3 unachievable and every later slice would otherwise be
written against globals and need reworking.

---

## 5. Tests to add (§13)

The suite is at **117 passed / 0 skipped**, and `tests/conftest.py` now prints a
skip banner so an incomplete run cannot read as a clean one. New coverage,
mapped to §13's required categories:

- one beat → multiple DirectorShots *(partly covered by `test_director.py`)*
- plan approval versioning; approval invalidation on material change
- Critic issue attaches to a specific shot or beat gap
- no paid generation before approval *(extend `test_paid_rebill.py` — these are
  the 5 money-path tests that only started running here on 2026-08-11)*
- generation idempotency under duplicate clicks / worker retry / network retry
- retry preserves prior attempts and ancestry
- failed generation → placeholder; placeholder → later output replaces in slot
- take swap preserves timeline slot identity and valid trims
- **project switch isolation and stale response rejection** — the C1 regression
  test; write it first, watch it fail, then land slice 1
- export snapshot immutability; master/FCPXML timeline equivalence

`test_paid_rebill.py` and `test_director.py` are the right models to follow:
behavioral, offline, and mutation-sensitive.

---

## 6. Open questions for Lucas

1. **C6 — Vesper.** Project-level setting defaulting to Vesper, or keep it
   fixed? Only place the contract and CLAUDE.md disagree.
2. **C2 — take storage.** Confirm the additive `GenerationAttempt` record over
   moving fields off `DirectorShot`. Moving them means touching the re-bill
   guard, which I would rather not disturb.
3. **Migration.** Existing projects (`bestiary/manananggal/` et al.) have plans
   without signatures and exports without snapshots. Migrate on read with
   defaults, or one-shot script?
4. **Scope.** Slices 0–4 deliver §15 items 1–8 and are the useful half. Is V1
   all eight slices, or do we ship after 5 (a watchable Draft 1) and treat
   Refine/Export as V1.1?
