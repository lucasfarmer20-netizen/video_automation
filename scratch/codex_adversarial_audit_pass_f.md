# FilmCraft V1 Adversarial Audit — Pass F: Project Isolation and Persistence

Audit date: 2026-08-11  
Audited state: current uncommitted working tree  
Posture: findings only; no implementation patches

## Confirmed defects

### F-01 — A request/job bound to Project B saves its storyboard over globally active Project A

- **Severity:** Critical
- **Confidence:** High
- **Area:** Manifest persistence / project authority
- **Contract requirement violated:** A request or job must write only to its bound project; job completion must use the job-owned project ID rather than the process-wide active project.
- **Exact reproduction steps:**
  1. Create Project A and Project B manifests with distinct titles.
  2. Point `.active_project` at A.
  3. Bind a `ProjectContext` for B.
  4. Call `save_current_project()` with B's storyboard.
  5. Reload both manifests.
- **Expected result:** B contains the new B state; A remains unchanged.
- **Observed result:** A is overwritten with B's state; B remains unchanged.
- **Evidence:**
  - `backend/main.py:260-262` correctly loads through `projects.bound()`.
  - `backend/main.py:311-320` then saves local JSON through `get_active_manifest_path()`, ignoring the bound context and explicitly overriding `manifest.save`'s safe default.
  - Reproduced twice. Output: `A=B-mutated-1, B=B-original-1`, then `A=B-mutated-2, B=B-original-2`.
- **Why it matters to the user:** Editing or completing work on one film can overwrite another film's authoritative manifest, causing wrong media selections, approval state, lineage, timing, and potentially unrecoverable work loss.
- **Reproduced more than once:** Yes, twice.
- **Suggested verification test:** With global active A and bound B, exercise `save_current_project` directly and through one real mutating HTTP endpoint plus one background job; assert byte-level non-mutation of A and persistence to B.
- **Fix direction:** Resolve the save path from `projects.bound()`/`config.manifest_path()` and use the same captured context for Firestore identity and local JSON. Do not consult the pointer once work is bound.

### F-02 — Background job namespace and status are global across projects

- **Severity:** High
- **Confidence:** High
- **Area:** Worker queue / status isolation / UI truthfulness
- **Contract requirement violated:** Concurrent project work must remain isolated; Project B must not be blocked by or shown Project A's job as its own.
- **Exact reproduction steps:**
  1. Bind Project A and start a blocking job named `shared-stage`.
  2. While it runs, bind Project B and start B's own `shared-stage` job.
  3. From B's bound context, read assembly job status.
- **Expected result:** B's job starts independently and B sees only B-owned job state.
- **Observed result:** B's `start_job` returns false because A owns the global name; B's status response contains A's job and A's `project_id`.
- **Evidence:**
  - `backend/pipeline_worker.py:17-18` stores jobs in one global dictionary keyed only by stage name.
  - `backend/pipeline_worker.py:46-48` rejects a same-named job without considering project identity.
  - `backend/pipeline_worker.py:93-103` returns every project's jobs without filtering.
  - `backend/main.py:4552-4554` exposes that complete registry to any bound request.
  - Reproduced twice with distinct job names; both times A started, B was rejected, and B saw A's project ID.
- **Why it matters to the user:** Two tabs/projects cannot safely run the same stage; one project receives another project's progress/error banners and may interpret completion as its own work.
- **Reproduced more than once:** Yes, twice.
- **Suggested verification test:** Start identical stage names concurrently under A and B; require both to start, require status responses to be filtered by bound project, and require logs/status transitions not to cross.
- **Fix direction:** Key jobs by `(project_id, logical_job_name)` and filter status by the bound context. Return an opaque job ID to clients where practical.

### F-03 — Several frontend reads omit project identity and stale-response rejection

- **Severity:** High
- **Confidence:** High
- **Area:** Frontend stale state / multiple tabs
- **Contract requirement violated:** A delayed response or another tab's project switch must not repaint the current project with another project's state.
- **Exact reproduction steps:**
  1. Open Project A in tab A and Project B in tab B, leaving the process-wide pointer on B.
  2. In tab A, run `fetchActiveProject()` or job polling.
  3. Observe requests for metadata, audio peaks, and job status.
- **Expected result:** Every project-scoped read carries A's ID and discards a response whose ID differs from A.
- **Observed result:** `/api/metadata`, `/api/audio/peaks`, and `/api/assemble/status` are fetched without `X-Project-Id`; the first two responses are applied without a stale check, while job status is both unscoped and applied wholesale.
- **Evidence:**
  - `frontend/src/app/page.tsx:198-207` performs unscoped metadata and peaks reads and immediately writes their payloads into A's UI state.
  - `frontend/src/app/page.tsx:217-251` performs unscoped job polling and installs the global registry.
  - The safe helper already exists at `frontend/src/app/page.tsx:307-311` but these calls bypass it.
- **Why it matters to the user:** The UI can show another film's metadata, waveform/timing information, or job outcome while the user is viewing A, creating false success and wrong editing decisions.
- **Reproduced more than once:** Deterministic request construction; F-02 independently reproduced the wrong-project job payload twice.
- **Suggested verification test:** Resolve A and B requests out of order in a component/integration test; assert no B payload reaches A state and every project-scoped fetch sends the explicit ID.
- **Fix direction:** Route all project-scoped GETs through the existing identity-aware/stale-aware helper and filter job payloads server-side as well.

## Probable defects

### PF-01 — Reference registries still bypass bound path helpers

- **Severity:** High
- **Confidence:** Medium
- **Area:** Reference persistence
- **Evidence:** `backend/assets.py:253-259` and `backend/main.py:416-422` read/write `config.REFERENCES_CONFIG`, a process global, rather than a bound-context helper. The same authority split as F-01 is present, but this pass did not drive the relevant endpoint end to end.
- **Risk:** A reference added or selected for B can be written into A's registry, associating generated output with the wrong creative reference.
- **Suggested verification test:** With global A and bound B, add/update a reference through the API and assert only B's `references.json` changes.

## Test gaps

### TG-F01 — Safe low-level manifest defaults mask the unsafe application save wrapper

- Existing `test_manifest_save_defaults_to_the_bound_project` verifies `manifest.save(sb)`.
- Production writes commonly call `save_current_project(sb)`, which passes an explicit global path and bypasses that protection.
- Add direct and HTTP/background coverage of the wrapper.

### TG-F02 — No cross-project same-stage concurrency/status test

- Existing tests prove that a uniquely named job captures A's context, but do not run the same logical stage under A and B or inspect B's filtered status view.

### TG-F03 — No multi-tab stale-response test for sidecar reads

- Existing HTTP tests verify response headers, but not whether every frontend consumer sends identity and rejects stale replies.

## Safeguards confirmed / rejected findings

### R-F01 — Worker thread context capture itself is effective

- **Not a finding:** `start_job` captures context at enqueue and rebinds it inside the thread. Removing that bind in an isolated mutation caused the core isolation test to fail: **1 failed, 18 passed**.

### R-F02 — Unknown explicit project IDs silently falling back

- **Not a finding:** `_context_for()` rejects unknown explicit IDs, and the HTTP tests verify a 404 rather than serving the active project.

### R-F03 — Bound config path helpers following the process pointer

- **Not a finding:** `config.manifest_path`, `project_dir`, `assets_dir`, and `references_dir` prefer `projects.bound()`. The defect is callers that bypass those helpers.

## Evidence summary

- Baseline focused isolation/stage/approval run: **65 passed, 0 skipped**.
- Worker-binding mutation: **1 failed, 18 passed**; the core capture test is meaningful.
- F-01 reproduced twice with real temporary manifests.
- F-02 reproduced twice with real background threads and distinct project contexts.
- No implementation files were changed; the isolated mutation directory was removed.

## Pass-F answer

The isolation claim is falsified. A bound B operation can overwrite A's manifest through `save_current_project`, and the job/status/UI layers still mix project ownership. Slice 1 should not gate Slice 2 until F-01 is fixed and mutation-sensitive end-to-end tests cover the application wrapper, not only the safe low-level primitives.

## Remediation re-verification — 2026-08-11

### Verdict

**F-01, F-02, F-03, and PF-01 are closed.** The remediated Slice 1 project-isolation safeguards passed independent reproduction and mutation testing. On the scope of Pass F, Slice 1 may gate Slice 2.

This verdict is limited to the isolation and persistence findings in this report. The working tree also contains unrelated stage/UI and earlier audit work; those changes were not accepted by this re-verification merely because the combined test suite passed.

### F-01 — closed

- `save_current_project()` now stamps the bound context's project ID before the Firestore save and calls `manifest.save(sb)` without an explicit path, allowing the bound-aware default to select the local manifest.
- Repeated the bound-B/global-A wrapper reproduction twice. Both runs preserved A and persisted the mutation to B.
- Mutation: restored the former explicit `get_active_manifest_path()` argument in an isolated copy. `test_save_current_project_writes_to_the_bound_project` failed because A was overwritten. The wrapper-level safeguard is mutation-sensitive.

### F-02 — closed

- The registry is keyed by `(project_id, logical_name)`; worker logging uses the captured project ID; status is filtered to one project.
- Repeated the same-stage A/B concurrency, status isolation, and log-buffer isolation reproduction twice. Both projects started independently and neither status/log view contained the other's state.
- Mutation: collapsed `_key()` back to a global logical name. The concurrent-start and log-isolation tests failed.
- Mutation: removed the project predicate from `get_jobs_status()`. The cross-project status test failed.

### F-03 — closed

- `/api/metadata`, `/api/audio/peaks`, and `/api/assemble/status` now use the identity-aware `getJson()` path, which sends `X-Project-Id` and drops replies stamped for a no-longer-current project.
- Rough-cut plan and motion reads use the same helper. Assembly status is also filtered server-side through `get_jobs_status()`.
- The budget-plan preview sends identity but directly consumes the response rather than calling `isStaleReply()`. This is a discrepancy from the implementation owner's remediation summary, but not a residual Pass-F defect: `/api/script/budget_plan` is a pure function of the request's `budget` and `beats` parameters and reads no project state. Its existing component effect also cancels superseded requests. It cannot return another film's data under the audited implementation.

### PF-01 — closed

- Reference and character registry callers now resolve through `config.references_config()`, `config.references_dir()`, and `config.characters_config()`.
- End-to-end path reproduction with global A and bound B confirmed that the main reference registry writer, asset reference reader, and character-anchor reader all targeted B; A remained unchanged.

### Independent evidence

- Focused isolation/stage/approval/manifest baseline: **89 passed, 0 skipped**.
- Exact F-01/F-02 reproduction group: **4 passed**, repeated twice.
- Complete project suite (`pytest -q tests`): **193 passed, 0 skipped**.
- TypeScript (`npx tsc --noEmit`): clean.
- Isolated mutation directory removed after testing; no production implementation file was edited by Codex.
