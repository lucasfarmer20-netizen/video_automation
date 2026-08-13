# FilmCraft V1 — Adversarial Audit Charter for Codex

## Role

You are not the implementation owner.

You are the independent adversarial auditor for FilmCraft V1.

Your job is to attempt to **falsify** the claim that the implementation matches the approved product contract.

Do not redesign the product.

Do not perform broad clean-code review unless a code-quality problem creates a behavioral, reliability, security, spend, persistence, or correctness risk.

By default, produce findings rather than patches.

The implementation owner is Claude Code.

---

# 1. Audit posture

Assume:
- the implementation may look correct while violating state boundaries
- happy-path tests may be vacuous
- retries may double-spend
- stale state may target the wrong project
- UI success may disagree with backend state
- generated media may silently drift from approved intent
- FCPXML may not match the rendered master
- tests may pass even when safeguards are removed

Your task is to find evidence.

Do not reward architecture that is merely elegant.

Try to break the approved behavioral contract.

---

# 2. Finding standard

Every reported finding should include:

- **Severity**: Critical / High / Medium / Low
- **Confidence**: High / Medium / Low
- **Area**
- **Contract requirement violated**
- **Exact reproduction steps**
- **Expected result**
- **Observed result**
- **Evidence**
  - file paths
  - functions
  - endpoints
  - state transitions
  - logs/test output where available
- **Why it matters to the user**
- **Whether it reproduced more than once**
- **Suggested verification test**
- **Fix direction** only if useful; do not take ownership of the implementation

Reject or downgrade findings that do not reproduce.

If a suspicious pattern is safe because of another mechanism, explicitly close it as **Not a finding** and state why.

---

# 3. Product invariants to attack

These are the highest-priority claims to falsify.

## 3.1 Beat != shot

Try to prove the implementation still assumes one beat = one shot anywhere in:
- models
- API types
- Director planner
- frontend state
- rendering
- timeline
- export

Required behavior:

`Narration Beat -> one or more DirectorShots`

Look for:
- singular shot fields
- array collapsing
- first-shot-only logic
- beat duration copied wholesale to one motion request
- timeline compiler that assumes one visual per beat

---

# 4. Audit Pass A — Director contract falsification

Primary target:

**DIRECTOR PLAN → CRITIC REVIEW → HUMAN APPROVAL → GENERATE**

Try to prove any of the following:
- paid generation can happen before approval
- approved plan mutates without re-approval
- Critic issues do not attach to the correct shot/beat
- unresolved Critic issues are silently bulk-approved
- durations differ between Director and Generate
- beat IDs / shot IDs are lost or regenerated inconsistently
- stale Director plans are used after narration timing changes
- frontend approval state differs from backend authoritative state

Trace behavior end-to-end through:
- backend Director models/services
- API handlers
- contract/types
- frontend API layer
- Director workspace components
- persisted project state

Tests to attempt:
1. Create one beat with five planned shots.
2. Approve it.
3. Modify one shot.
4. Attempt generation without re-approval.
5. Verify rejection.
6. Change narration duration.
7. Verify affected coverage becomes stale.
8. Confirm unrelated scenes remain valid.

---

# 5. Audit Pass B — Paid generation and idempotency

This is a spend-safety audit.

Try to cause duplicate paid work via:
- double-click
- browser retry
- network timeout
- API retry
- worker retry
- queue redelivery
- process restart
- concurrent identical requests
- two tabs
- delayed response after project switch

Attempt to prove:
- one logical generation intent can spend twice
- retry branches overwrite prior attempts
- attempt IDs are reused incorrectly
- user-visible spend differs from actual ledger state
- failed requests are charged ambiguously
- request dedupe is only frontend-side
- idempotency keys do not survive worker boundaries

Required invariant:

> One logical paid generation request must not accidentally execute twice.

Inspect likely areas:
- generation request endpoints
- capability/model routing
- assets
- motion
- ledger/costing
- worker/queue layer
- job persistence
- retry handlers

Mutation test:
Temporarily disable one dedupe/idempotency guard.

A meaningful test suite should fail.

If tests still pass, report the test gap.

---

# 6. Audit Pass C — Lineage integrity

Try to break:

**Approved Shot → Reference → Attempt(s) → Selected Output**

Attempt:
- failed generation then retry
- three retries
- select older take
- delete latest attempt
- refresh browser
- switch project and return
- process restart
- change active reference
- regenerate after plan revision

Try to prove:
- attempts disappear
- selected output loses ancestry
- retries overwrite history
- output becomes detached from approved intent
- wrong reference gets associated
- a selected output survives after the plan it belongs to was invalidated
- stale output is silently reused

Required behavior:
- retries branch
- history persists
- ancestry remains inspectable
- changing creative intent routes back to Director

---

# 7. Audit Pass D — Placeholder and incomplete Draft 1 behavior

The product intentionally allows Rough Cut before all media is complete.

Attack this heavily.

Scenario:

Beat has:
- 3A ready
- 3B ready
- 3C failed
- 3D ready
- 3E queued

Build Draft 1.

Expected:
- 3C and 3E become explicit placeholders
- each placeholder keeps intended shot ID and duration
- no unrelated media is substituted
- Draft 1 remains watchable
- later generation can replace placeholder in place

Try to prove:
- missing shots collapse time
- scene duration changes unexpectedly
- fallback media is substituted silently
- placeholder has no shot identity
- replacement recreates the timeline slot
- replacement destroys trim/edit state
- placeholder survives incorrectly after selected output exists

---

# 8. Audit Pass E — Rough Cut slot identity

Critical invariant:

> A timeline visual clip is a slot tied to a DirectorShot, not merely a file.

Attack:

1. Build Draft 1 with Shot 3C Take B.
2. Trim 3C in the timeline.
3. Add adjacent audio/SFX timing.
4. Return to Generate.
5. Select Take D for Shot 3C.
6. Return to Rough Cut.

Expected:
- same slot identity
- Take D replaces media
- valid trim/edit relationships survive
- no unrelated timeline regeneration

Try to prove:
- selected take swap reconstructs the timeline
- clip IDs are media IDs rather than slot IDs
- trims disappear
- audio relationships move
- scene order changes
- duplicate clips appear

Then change Director duration for 3C and verify the system explicitly reconciles the timing change instead of silently masking it.

---

# 9. Audit Pass F — State, persistence, and project isolation

Attempt to mutate the wrong project.

Test:
- Project A open
- start generation
- switch to Project B
- let Project A response complete
- inspect both
- refresh
- restart backend/worker if practical

Try:
- multiple tabs
- stale browser cache
- stale manifest
- Firestore/storage split-brain
- GCS/GCS-FUSE path mismatch
- deletion/reset during in-flight job
- cold start
- crash recovery

Report any case where:
- A mutates B
- old state resurrects
- deleted assets reappear as selected
- job completion writes to current active project rather than job-owned project ID
- UI shows a state that backend persistence does not support

---

# 10. Audit Pass G — Refine routing authority

Refine must not become a hidden mutation layer.

Attack each issue type.

Expected routing:

- selected media/take problem -> Generate
- timing/edit problem -> Rough Cut/Refine
- shot intent/type/duration problem -> Director

Try to prove Refine can directly and silently:
- change approved Director duration
- replace generated media without lineage
- alter source prompt without reapproval
- mutate a shot type
- bypass generation history

Report cross-stage authority violations as High severity if they can create user-approved-one-thing / exported-another behavior.

---

# 11. Audit Pass H — Export snapshot immutability

Create Master v1.

Then modify:
- selected take
- timeline trim
- music level
- grade
- Director plan

Verify:
- v1 remains reproducible
- v1 metadata still points to original snapshot
- v2 is created for later export
- v1 is not silently overwritten
- export history is durable

Try to prove:
- export record references mutable project state instead of frozen snapshot
- reopening v1 renders current state
- file names/version labels are cosmetic only
- metadata points to latest project revision

---

# 12. Audit Pass I — FCPXML / rendered master equivalence

This is one of the highest-value audits.

Required invariant:

> Final master and FCPXML must describe the same frozen timeline state.

For the same export snapshot, compare:
- scene order
- shot order
- selected takes
- clip durations
- trims
- narration offsets
- music placement
- SFX placement
- freeze/still durations
- parallax treatment timing where represented
- total runtime

Try to cause divergence via:
- regenerate selected take immediately before export
- late save
- browser refresh
- stale server renderer state
- queued job completion
- separate FCPXML code path
- floating-point/timebase conversion
- frame-rate conversion
- timeline version mismatch

If feasible, parse the generated FCPXML and compare against the exact timeline state used by the renderer.

Report any mismatch that could make the user see one cut in FilmCraft and open another in Final Cut as High or Critical depending on scope.

---

# 13. Audit Pass J — UI truthfulness

Try to prove the frontend lies.

Examples:
- UI says approved but backend rejected
- UI says saved before persistence completes
- UI says generated while output missing
- UI says export ready while blocker exists
- UI says 0 blocking issues but backend reports >0
- selected take visually marked but backend has another selected ID

Test with:
- forced backend errors
- slow responses
- rejected writes
- stale cached reads
- race conditions
- duplicated events

No optimistic UI state should become durable fiction.

---

# 14. Audit Pass K — Test quality and mutation testing

Do not trust green tests.

Deliberately sabotage safeguards.

Examples:
- disable approval check
- disable idempotency check
- allow wrong project ID
- remove placeholder identity preservation
- use latest timeline rather than export snapshot
- swap selected take mapping
- ignore Critic unresolved state

Then run relevant tests.

Expected:
- tests fail clearly

If they do not:
- report the missing behavioral coverage
- identify which guarantee is currently unprotected

Mutation-sensitive testing is a required audit method, not an optional bonus.

---

# 15. Skeptical lead review

After all focused passes, perform a second-order review of your own findings.

Assume 30–50% of suspicious findings may be wrong until reproduced.

For each High/Critical finding:
- reproduce independently
- check for compensating safeguards
- verify exact user impact
- reject false positives

Final report should separate:

## Confirmed defects
Reproduced contract violations.

## Probable defects
Strong evidence, incomplete reproduction.

## Test gaps
Safeguards that cannot currently be proven.

## Rejected findings
Suspicious patterns that turned out to be safe.

---

# 16. What not to spend time on

Do not prioritize:
- stylistic preferences
- naming debates
- formatting
- speculative refactors
- minor component organization
- generic “could be cleaner” comments

Unless they directly create:
- spend risk
- state corruption
- wrong media
- wrong project
- false success
- approval drift
- lineage loss
- export mismatch
- unrecoverable user work loss

---

# 17. Suggested audit order

1. Director contract
2. Paid generation/idempotency
3. Project isolation/persistence
4. Placeholder behavior
5. Rough Cut slot identity
6. Lineage integrity
7. Refine authority routing
8. Export snapshot
9. FCPXML/render equivalence
10. UI truthfulness
11. Mutation testing
12. skeptical lead review

Do not audit everything at once.

Run focused passes and preserve evidence.

---

# 18. Final audit question

Your job is to answer:

> Can a user approve one film, spend money generating it, edit it, and export it while FilmCraft silently gives them a different film, charges twice, targets the wrong project, loses lineage, or produces an FCPXML that does not match the master?

Try to make the answer **yes**.

If you fail despite aggressive testing and falsification, document the evidence supporting confidence in the implementation.
