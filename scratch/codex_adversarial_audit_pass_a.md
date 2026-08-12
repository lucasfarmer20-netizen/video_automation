# FilmCraft V1 Adversarial Audit — Pass A: Director Contract

Audit date: 2026-08-11  
Audited state: current working tree (including uncommitted implementation-owner changes)  
Posture: findings only; no implementation patches

## Confirmed defects

### A-01 — Draft Director plans can bypass approval and enter generation

- **Severity:** High
- **Confidence:** High
- **Area:** Director API / generation gate
- **Contract requirement violated:** `DIRECTOR PLAN -> CRITIC REVIEW -> HUMAN APPROVAL -> GENERATE`; paid generation must not happen before approval.
- **Exact reproduction steps:**
  1. Create a valid Director coverage plan with `status="draft"` and one generated/parallax shot.
  2. Ensure the storyboard is approved (isolating the Director-plan approval boundary).
  3. Call `POST /api/director/compile/{beat_id}?force=true`.
  4. Observe the API response and the plan status captured when the job is accepted.
- **Expected result:** A draft plan is rejected until it is explicitly locked/approved.
- **Observed result:** HTTP 200 with `started: true`; the accepted job still owns a plan whose status is `draft`.
- **Evidence:**
  - `backend/main.py:2506` exposes the compile endpoint.
  - `backend/main.py:2514` explicitly bypasses the draft rejection when `force=true`.
  - `backend/main.py:2535` passes that draft plan into `director.compile_coverage`.
  - `backend/director.py:815-878` can generate missing stills and paid video while compiling coverage.
  - Reproduced twice through `fastapi.testclient.TestClient`; both calls returned HTTP 200 and captured `('director:s001', 'draft')` at job acceptance.
- **Why it matters to the user:** A plan that the user has not approved can create media/spend and can produce a film different from the approved intent.
- **Reproduced more than once:** Yes, twice.
- **Suggested verification test:** An HTTP test should assert that every draft compile request, including `force=true`, returns 409 and never invokes `start_job`, still generation, or paid generation.
- **Fix direction:** Remove the public force bypass or constrain it to a separately authorized recovery operation that still requires an immutable approval record matching the plan revision.

### A-02 — Unresolved Critic issues can be silently bulk-locked

- **Severity:** High
- **Confidence:** High
- **Area:** Director approval / Critic contract
- **Contract requirement violated:** Critic review must precede meaningful human approval; unresolved issues must not be silently bulk-approved.
- **Exact reproduction steps:**
  1. Save a valid draft coverage plan containing an unresolved warning attached to `s001.01`.
  2. Call `POST /api/director/lock_scene` with `{"beats":["s001"]}`.
  3. Reload the persisted plan.
- **Expected result:** Lock is rejected until each unresolved warning is resolved or explicitly acknowledged with a durable human decision.
- **Observed result:** HTTP 200; persisted plan status becomes `locked`; the unresolved warning remains present.
- **Evidence:**
  - `backend/main.py:2331-2352` validates duration/shape only and then locks; it does not inspect `plan.warnings`.
  - Reproduced twice; each response returned `locked: ['s001']`, and each saved plan retained one warning.
- **Why it matters to the user:** The Generate stage can proceed despite the review stage reporting a known problem, turning “approved” into an ambiguous and potentially misleading state.
- **Reproduced more than once:** Yes, twice.
- **Suggested verification test:** Seed shot- and beat-scoped warnings (including stale warnings), call both lock endpoints, and assert rejection unless a durable per-warning resolution/override record exists.
- **Fix direction:** Make warning disposition authoritative and persisted; require resolved or explicitly overridden issue IDs as part of lock validation.

### A-03 — Dismissing a Critic warning makes the UI disagree with backend truth

- **Severity:** Medium
- **Confidence:** High
- **Area:** Director frontend state / UI truthfulness
- **Contract requirement violated:** Frontend approval/review state must agree with authoritative backend state; the UI must not say zero issues when the backend retains issues.
- **Exact reproduction steps:**
  1. Open a plan with one Critic warning.
  2. Trigger the warning's resolve/dismiss action.
  3. Observe the warning count in the workspace.
  4. Refresh/refetch the plan or inspect the persisted plan.
- **Expected result:** Resolution is persisted, or the UI clearly labels dismissal as local/non-authoritative and does not claim the issue is resolved.
- **Observed result:** The warning is removed only from local `coveragePlan.warnings`; no backend request is made. The persisted warning therefore remains and returns on refresh.
- **Evidence:**
  - `frontend/src/components/DirectorWorkspace.tsx:269-275` filters the warning from React state only.
  - No warning-resolution endpoint exists in `frontend/src/lib/directorApi.ts`; the only Critic write reruns critique.
  - The warning summary at `frontend/src/components/DirectorWorkspace.tsx:594-600` is driven by that local array.
- **Why it matters to the user:** The screen can report that review is clear while authoritative project state says otherwise; refresh resurrects supposedly resolved work.
- **Reproduced more than once:** Deterministic code path; backend persistence mismatch independently reproduced twice in A-02.
- **Suggested verification test:** A component/API integration test should dismiss a warning, reload the scene, and require the same resolved state after reload.
- **Fix direction:** Persist resolution/override through a backend endpoint, then replace local state with the server response.

## Test gaps

### TG-01 — Director compile approval guard is not mutation-sensitive

- **Severity:** High (coverage gap)
- **Confidence:** High
- **Guarantee unprotected:** A draft Director plan must never compile or generate.
- **Mutation:** In an isolated copy, replaced `if plan.status == "draft" and not force:` with an always-false condition.
- **Observed result:** The focused Director/gate suites still passed: **59 passed, 0 skipped**.
- **Suites run:** `test_director.py`, `test_director_shot_patch.py`, `test_gates_and_writes.py`, `test_paid_rebill.py`.
- **Suggested test:** Exercise the HTTP compile endpoint with draft/locked/compiled states and both values of `force`; assert job dispatch and generation calls, not merely response text.

### TG-02 — No behavioral coverage for Critic warning disposition at lock

- **Severity:** High (coverage gap)
- **Confidence:** High
- **Guarantee unprotected:** Unresolved Critic issues cannot be silently bulk-approved.
- **Evidence:** Repository test search found no lock-scene test that seeds warnings and verifies disposition or durable acknowledgment.
- **Suggested test:** Parameterize beat-scoped, shot-scoped, stale, resolved, and explicitly overridden warnings across both lock endpoints.

## Rejected findings / safeguards confirmed

### R-01 — Editing a locked shot through the sanctioned patch endpoint

- **Not a finding:** `POST /api/director/shot/{shot_id}` rejects edits unless the plan is in draft status, preventing the ordinary edit route from silently mutating an approved plan.

### R-02 — Narration-duration drift reaching compile

- **Not a finding:** `director.validate` compares the plan's duration snapshot with the live beat duration before compilation and rejects drift. Existing focused tests exercise this safeguard.

### R-03 — One beat being collapsed to one Director shot in the core contract

- **Not a finding for the audited path:** Backend and frontend Director models carry `coverage: DirectorShot[]`; validation and compilation iterate all coverage shots in order. The focused suite passed multi-shot persistence and assembly checks.

## Audit evidence summary

- Baseline focused run: **78 passed, 0 skipped**.
- Mutation run: **59 passed, 0 skipped** despite removal of the draft compile guard.
- No production implementation files were changed.
- The isolated mutation directory was deleted after the run.

## Pass-A answer

For the Director boundary, the answer to the charter's final question is **yes**: a caller can send an unapproved draft plan into generation using the public force flag, and unresolved Critic issues can coexist with a persisted locked/approved state. The current tests do not protect either guarantee.

## Remediation verification — 2026-08-11

The implementation owner remediated A-01 through A-03. Independent verification found:

- **A-01 closed:** the compile endpoint no longer accepts a force parameter and rejects draft plans before job dispatch.
- **A-03 closed:** warning decisions are written through a backend endpoint and the UI replaces its state with the authoritative response.
- **TG-01 closed:** disabling the compile guard caused 6 focused test failures.
- **TG-02 materially improved:** ignoring warning disposition caused 6 focused test failures.
- **Warning identity mutation coverage confirmed:** replacing the content hash with a constant caused 2 focused test failures.
- Verification suite: **103 passed, 0 skipped**.

### A-04 — A Critic-supplied warning ID can carry an old decision onto a changed finding

- **Severity:** High
- **Confidence:** High
- **Area:** Director Critic warning identity / approval
- **Contract requirement violated:** A finding whose content or target changed must receive a new identity and require a new human decision; unresolved Critic issues must not be silently approved.
- **Exact reproduction steps:**
  1. Normalize a warning with `id="critic-fixed-id"`, target `s001.01`, and detail `old problem`.
  2. Record an `accepted` disposition for `critic-fixed-id`.
  3. Replace the warning with one carrying the same supplied ID but a different target, kind, and detail.
  4. Normalize it and call `unresolved_warnings`.
- **Expected result:** The changed content produces the derived ID for the new finding and is unresolved.
- **Observed result:** The supplied ID is retained and the old disposition applies; unresolved count is zero. Independently reproduced output included derived hashes `6ae4ca281715` and `946fbeb1da24`, while both normalized warnings retained `critic-fixed-id`.
- **Evidence:**
  - `backend/director.py:227` uses `d.get("id") or warning_id(d)`, trusting an incoming ID instead of deriving identity from authoritative content.
  - Existing tests construct warnings without IDs, so they do not exercise this path.
- **Why it matters to the user:** A changed Critic finding can be treated as already reviewed, allowing lock and generation without a human deciding the new issue.
- **Reproduced more than once:** Deterministic direct-domain reproduction; the same mechanism is exercised on every normalization.
- **Suggested verification test:** Re-critique with two materially different warnings that deliberately carry the same incoming ID; assert the second receives a content-derived ID, loses the old disposition, and blocks lock/compile.
- **Fix direction:** Always compute the canonical ID from the identity-bearing content. If an external/provider ID is useful, preserve it under a separate field such as `source_id`; never use it as the disposition key.
