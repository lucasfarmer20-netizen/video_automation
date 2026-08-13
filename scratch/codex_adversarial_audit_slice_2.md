# FilmCraft V1 Adversarial Audit — Slice 2: Plan Signature and Approval

Audit date: 2026-08-11  
Audited commit: `a419310` (`filmcraft/slices-0-1`, pushed and unmerged)  
Posture: findings only; no implementation patches

## Verdict

**Slice 2 is not safe to gate Slice 3.** The approval signature can collide for materially different, user-controlled plan fields, allowing an edited plan to inherit an approval it was never given. Approval migration and invalidation also claim durable provenance but do not persist it.

## Confirmed defects

### S2-01 — Delimiter ambiguity lets a materially changed plan retain approval

- **Severity:** Critical
- **Confidence:** High
- **Area:** Plan identity / approval gate
- **Contract violated:** §5.4 and §11.5 require approval to bind to the exact material plan.
- **Cause:** `plan_signature()` stringifies fields and joins them with `|`, but does not escape or length-prefix field values. User-controlled strings may themselves contain `|`, so field boundaries are ambiguous before hashing.
- **Reproduction:** Plan A used `purpose="alpha|beta"`, `subject="gamma"`; plan B used `purpose="alpha"`, `subject="beta|gamma"`. All other fields were identical. The plans produced the same signature (`07a27081977420b2`). Copying A's approved signature to B made `approval_is_current(B)` return true.
- **Impact:** A materially edited plan can pass the compile approval gate under another plan's approval. This is a deterministic serialization collision, not a cryptographic SHA-256 collision.
- **Suggested fix direction:** Hash a canonical structured encoding with explicit field names and boundaries, such as deterministic JSON (`sort_keys=True`, fixed separators) over a versioned material-plan object. Do not build the preimage by delimiter concatenation.
- **Required regression:** Construct the exact boundary-shift pair above and assert different signatures, then exercise the compile endpoint and assert no job dispatch for the edited plan. Mutation-test by restoring delimiter concatenation.

### S2-02 — Migration and drift invalidation are not persisted on read

- **Severity:** High
- **Confidence:** High
- **Area:** Approval provenance / persistence
- **Contract violated:** Slice 2 promises preserved approval history and an idempotent migration that writes provenance for pre-signature locks.
- **Cause:** `load_plan()` mutates the in-memory `CoveragePlan` but never calls `save_plan()` after adopting a migrated signature or invalidating a stale one.
- **Migration reproduction:** Saved a legacy-style locked plan without a signature, then loaded it. The returned object had a current migrated signature, but the JSON file still had no `approved_signature` or migration provenance.
- **Drift reproduction:** Saved an approved locked plan, edited a material field directly in JSON, then loaded it. The returned object was draft with one history entry; the file remained locked, retained the stale signature, and contained zero history entries.
- **Impact:** The authoritative file continues to assert stale locked state, invalidation history is not durable, and every read reconstructs a transient audit record rather than preserving the actual transition. A reader that does not go through `load_plan()` can still observe the false locked state. The implementation owner's stated first-load write does not occur.
- **Suggested fix direction:** Make migration/invalidation an explicit persisted transition under the same project/plan write discipline, ideally with an atomic replace and a test that rereads the raw file after a single load. If reads must remain side-effect free, perform a one-time migration separately and persist invalidation at the mutation boundary instead; do not claim migrate-on-read persistence.

## Additional boundary risk

The signature covers shot-level production fields and timing but omits all plan-level fields except `beat_id` and `beat_duration`, including `plan_id`, `scene_beats`, `profile`, `visual_strategy`, and `blocking`; it also omits shot `library_scope`. Some are rationale or workflow metadata today, but `library_scope` is explicitly recorded for later resolution and `plan_id` is used as generation provenance. The material-field contract should be stated and tested at the plan level before these fields acquire stronger runtime effects. This is not raised as a separate blocker because S2-01 already falsifies exact-plan identity and the present compile path does not use most of these fields to determine pixels or cost.

## Safeguards confirmed

- Locking both a beat and a scene calls `approve()`.
- Unlocking clears current approval fields.
- `load_plan()` returns drifted plans as draft, so the current compile endpoint refuses ordinary drift before dispatch.
- Draft refusal and drift refusal are behaviorally distinguished; focused tests assert the branch result and dispatch side effect rather than status code alone.
- Produced-state changes such as take selection do not invalidate approval under the current material-field policy.
- Draft plans are not assigned migrated approvals.

## Test evidence

- Focused approval/compile baseline: **57 passed, 0 skipped**.
- Complete suite: **223 passed, 0 skipped**.
- Independent delimiter-collision reproduction: materially different plans shared a signature and approval.
- Independent raw-file reproduction: migration and invalidation changed only the returned object, not persisted JSON.
- No production implementation files were changed by Codex.

## Remediation re-verification — 2026-08-11

Audited commit: `2a3b0de`

### Original findings

- **S2-01 closed.** The signature preimage is now a canonical JSON object keyed by field name and carrying `SIGNATURE_VERSION`. The exact `purpose="alpha|beta"` / `subject="gamma"` boundary-shift pair no longer matches its shifted counterpart, and the edited plan cannot borrow the original approval through the compile endpoint.
- **S2-02 closed for ordinary serial reads.** Migration is persisted on the transitioning read; drift is persisted as draft with the signature cleared and one history entry; later settled reads do not rewrite the file.
- The material/non-material classification now accounts for every `DirectorShot` and `CoveragePlan` dataclass field. `library_scope` is material.
- Faithfully restoring the original delimiter-concatenated preimage caused **5 failures**, including the end-to-end compile test.
- Disabling transition persistence caused **3 failures** in the migration, invalidation, and once-only-write tests.

### S2-03 — Shared temporary filename makes concurrent plan saves fail

- **Severity:** High
- **Confidence:** High
- **Area:** Plan persistence / concurrency
- **Status:** Open; remediation blocker
- **Cause:** `save_plan()` always writes `<beat>.json.tmp` and then replaces `<beat>.json`. Two writers for the same beat share that temporary pathname. Atomic replacement protects a single writer from exposing partial JSON; it does not make competing writers safe.
- **Reproduction:** Bound one project context and launched two real threads that simultaneously called `save_plan()` for `s001`. On the first round, one writer failed with Windows `PermissionError [WinError 32]` replacing `s001.json.tmp` because the other writer held or moved that same file.
- **Impact:** A concurrent approval migration/invalidation or compile/update can fail to persist. `load_plan()` catches the resulting `OSError` and returns the transitioned object anyway, so the caller can observe a current approval state while the authoritative file remains stale—the persistence half of S2-02 can reappear under concurrency.
- **Test gap:** `test_a_plan_write_is_atomic` only asserts that no `*.tmp` file remains after one serial save. It does not run concurrent writers, verify that neither raises, or verify that the final file is valid and corresponds to a complete submitted state.
- **Suggested fix direction:** Use a unique temporary file created in the destination directory for each write, flush/fsync as appropriate, and serialize or add optimistic concurrency/version checks for same-beat logical updates. Unique temp files prevent the immediate pathname collision; they do not by themselves prevent last-writer-wins lost updates.

### Re-verification evidence

- Focused approval/compile suite: **72 passed, 0 skipped**.
- Complete suite: **238 passed, 0 skipped**.
- Direct serial reproductions for S2-01 and S2-02 passed.
- Concurrent same-beat save reproduction failed immediately with `PermissionError`.
- Isolated mutation directory removed; no production implementation file was edited by Codex.

### Updated verdict

The two reported remediation targets are closed in serial operation, and their regression tests are mutation-sensitive. **Slice 2 remains blocked by S2-03**, introduced by the persistence remediation: the claimed atomic save is not safe for concurrent writers to the same beat.

## S2-03 remediation re-verification — 2026-08-11

Audited commit: `c512510`

### Result

**S2-03 remains open.** The shared temporary pathname was removed and same-beat writes are now serialized with an in-process per-path lock, but sustained concurrent saving still loses writes on Windows during `os.replace`.

### Evidence

- The committed regression passed **10 consecutive executions** at its configured load (8 writers × 25 saves).
- A stronger independent run (20 batches, each 16 writers × 20 saves to `s001`) failed in batch 2 while the lock was active. One writer raised `PermissionError [WinError 5]` replacing its unique temporary file onto `s001.json`.
- The surviving destination remained valid, complete JSON and no temporary files remained. The defect is loss/failure of a submitted save, not torn output.
- Removing the per-file lock in an isolated mutation caused both primary concurrency tests to fail immediately. The new regression is mutation-sensitive and the lock materially improves behavior; it is not sufficient to guarantee the stated no-failure contract under heavier contention.
- Focused approval/compile suite: **77 passed, 0 skipped**.
- Complete suite: **243 passed, 0 skipped**.
- Isolated mutation directory removed; no production implementation file was edited by Codex.

### Why it remains a blocker

`load_plan()` still catches an `OSError` from transition persistence and returns the safe in-memory state while disk retains the old state. A transient replace denial therefore recreates the file/process disagreement S2-03 is meant to close. The in-process lock prevents simultaneous calls from entering `os.replace`, but Windows can still transiently deny replacement of the destination under rapid churn.

### Verification target

Add bounded retry/backoff for retryable Windows sharing/access violations while holding the per-file lock, with cleanup preserved. The regression should exercise sustained contention across repeated batches, assert every writer completes, assert the destination is one complete submitted state, and assert no temporary files remain. Cross-process and lost-update semantics may remain explicitly out of scope, but an accepted in-process `save_plan()` call must not be silently lost to a transient replace denial.

## S2-03 final remediation re-verification — 2026-08-11

Audited commit: `8fda625`

### Final verdict

**S2-03 is closed. Slice 2 may gate Slice 3.** `os.replace` now retries, while holding the per-plan lock, only for Windows access-denied/sharing-violation errors (5 and 32), with eight bounded attempts and exponential backoff. Non-retryable errors propagate immediately and exhausted transient errors still fail rather than spin forever.

### Independent evidence

- Repeated the stronger load that previously exposed S2-03: **3 cycles / 60 batches / 19,200 same-beat saves**, with zero exceptions, hangs, torn/spliced files, or temporary-file leaks.
- The focused approval/compile suite passed: **81 passed, 0 skipped**.
- The complete project suite passed: **247 passed, 0 skipped**.
- Removing `_replace_with_retry` in an isolated mutation caused both deterministic retry tests to fail: transient denial no longer recovered, and bounded-attempt accounting fell from 8 calls to 1. The safeguard is mutation-sensitive.
- The isolated mutation directory was removed; no production implementation file was edited by Codex.

Two initial baseline commands timed out because their child pytest processes survived the command timeout and competed with later runs. Codex identified and terminated only those four processes; after cleanup the same focused suite completed in 6.24 seconds and the complete suite in 16.97 seconds. This was audit-runner contamination, not a product defect.

### Scope retained

The accepted guarantee is in-process atomic, complete saves without loss from transient Windows replace denial. Cross-process coordination and logical lost-update prevention remain explicitly out of scope and are not implied by this closure.
