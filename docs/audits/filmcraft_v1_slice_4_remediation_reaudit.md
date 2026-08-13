# FilmCraft V1 — Slice 4 remediation re-audit

Audit date: 2026-08-12  
Audited branch: `main`  
Audited commit: `ad0dab2`  
Implementation diff: `e2bdf2f..d515320`  
Posture: findings only; no production patches

Governing documents:

- `docs/FILMCRAFT_V1_CODEX_ADVERSARIAL_AUDIT_CHARTER.md`
- `docs/FILMCRAFT_V1_CODE_IMPLEMENTATION_CONTRACT.md`

## Verdict

The remediation closes the original S4-01, S4-02, and S4-03 financial-corruption
paths under the documented single-process deployment model.

The central `in_flight` claim survived adversarial testing: no production path
was found where duplicate requests for one shot and signature reach the paid
provider twice.

Slice 4 still has two liveness defects and one terminal-transition defect:

1. An ordinary uncertain provider failure permanently strands the beat because
   `abandon()` has no API or UI.
2. Failures before the provider call can create the same permanent `RUNNING`
   state despite there being no billing uncertainty.
3. Conflicting terminal callbacks with the same status are silently accepted as
   identical replays.

## Confirmed defects

### S4-R01 — Ordinary paid failures permanently strand the beat

- **Severity:** High
- **Confidence:** High
- **Area:** Paid generation recovery
- **Contract requirements violated:** Product goal §1 prohibits hidden manual
  pipeline steps and knowledge of storage internals; Generate §6 requires users
  to survive failures and retries without losing intent; definition of done §8
  requires failure/retry survival without double-spending.

#### Reproduction

1. Create and approve a locked `ai_video` Director shot.
2. Make `generate_paid_clip()` raise
   `RuntimeError("provider timeout after submit")`.
3. Compile the beat.
4. Observe one attempt persisted as `RUNNING`, with the timeout recorded.
5. Compile the same beat again.
6. Observe `in_flight` refusal and no second provider call.
7. Inspect all registered FastAPI routes.

#### Expected

Automatic retry is refused, but an authenticated, project-scoped human recovery
operation is available.

#### Observed

- First compile failed.
- Second compile failed as `in_flight`.
- Provider call count remained one.
- No registered route exposes generation recovery or `abandon()`.
- The only recovery is calling `generation.abandon()` from internal Python.

Evidence from two independent reproductions:

```text
run 1 provider_calls 1 status running error provider timeout after submit
run 2 provider_calls 1 status running error provider timeout after submit
abandon_routes []
```

Relevant code:

- `backend/director.py:1282-1289`
- `backend/director.py:1307-1316`
- `backend/generation.py:275-306`

#### User impact

A normal provider timeout can make an approved beat permanently uncompilable
through the product. Recovery requires shell access and internal implementation
knowledge.

#### Reproduced more than once

Yes, independently in two isolated projects.

#### Suggested verification test

Drive the failure through `POST /api/director/compile/{beat_id}`, then inspect
and abandon the attempt using only supported API/UI operations before retrying
successfully.

#### Fix direction

Expose an authenticated, project-bound recovery workflow that shows the
uncertain attempt and requires an explicit human decision before abandoning it.

---

### S4-R02 — A pre-provider filesystem failure creates a false, reasonless in-flight attempt

- **Severity:** High
- **Confidence:** High
- **Area:** Paid-attempt state boundary
- **Contract requirements violated:** Generate §6 requires usable and
  inspectable failure/retry lineage; safety §11.4 requires authoritative state
  to agree with what occurred; definition of done §8 requires generation to
  survive failures.

#### Cause

`generation.begin()` persists `RUNNING` before inspecting, logging, and deleting
an existing target. Only `generate_paid_clip()` is inside the `in_doubt()`
exception handler.

#### Reproduction

1. Put an unrelated/free file at the paid shot's target path.
2. Compile the approved paid shot.
3. Inject `PermissionError` from `target.unlink()`, before the provider call.
4. Inspect lineage.
5. Retry the compile.

#### Expected

Because the provider was never called, the attempt closes as an ordinary
pre-dispatch failure or is safely retryable.

#### Observed

- Provider calls: zero.
- Attempt status: `RUNNING`.
- Attempt error: empty.
- First compile: `PlanError`.
- Every retry: `PlanError` through `in_flight`.
- No product-accessible recovery exists because of S4-R01.

```text
run 1 provider 0 rows [('running', '')] outcomes ['PlanError', 'PlanError']
run 2 provider 0 rows [('running', '')] outcomes ['PlanError', 'PlanError']
```

Relevant code:

- `backend/director.py:1277`
- `backend/director.py:1298-1306`
- `backend/director.py:1307-1316`

#### User impact

A storage deletion or permission error on Linux/GCS-FUSE can permanently strand
a beat even though no paid request reached the provider. The ledger misleadingly
represents this as possible billing uncertainty and records no cause.

#### Reproduced more than once

Yes, twice in separate projects.

#### Suggested verification test

Parameterize failures after `begin()` but before provider dispatch—target stat,
logging, and deletion—and require zero provider calls, a terminal pre-dispatch
failure with a reason, and a safe retry.

#### Fix direction

Establish an explicit provider-dispatch boundary. Pre-dispatch work should occur
before opening a billable attempt or should close the attempt as a known
failure. Only exceptions after dispatch should remain in doubt.

---

### S4-R03 — Same-status terminal conflicts are treated as identical callbacks

- **Severity:** Medium
- **Confidence:** High
- **Area:** Terminal lineage integrity
- **Contract requirements violated:** Generate §6 requires authoritative
  attempt-level cost and history; safety §11.6 requires preserved lineage; the
  remediation contract says only identical terminal transitions are no-ops.

#### Cause

`_finish()` checks only `target.status == status`. It does not compare output,
cost, or error.

#### Sequential reproduction

1. Succeed an attempt with `first.mp4`, `$0.60`.
2. Succeed it again with `other.mp4`, `$99.00`.

The second call returned successfully instead of raising `TerminalConflict`.
The first result remained stored.

```text
same_status 1 returned_without_conflict first.mp4 0.6
same_status 2 returned_without_conflict first.mp4 0.6
```

#### Concurrent reproduction

Race `fail("provider failed")` against
`abandon("human abandoned")` 100 times.

- Both callers reported success in all 100 races.
- The stored reason depended on which thread won:

```text
abandoned: human abandoned  26 times
provider failed             74 times
```

#### Expected

The first terminal result wins; a callback carrying different terminal facts
receives `TerminalConflict`. Only an exact replay is a no-op.

Relevant code: `backend/generation.py:255-264`.

#### User impact

Callers can believe their terminal result was recorded when it was silently
discarded. Conflicting output, cost, and failure disposition become invisible,
weakening billing and recovery auditability.

#### Reproduced more than once

Yes: two sequential reproductions and 100 concurrent races.

#### Suggested verification test

Cover success with different output, success with different cost, failure with
different error, failure versus abandon, and an exact byte-for-byte replay.

## Test gaps

### TG-S4-01 — No supported recovery test

Existing tests recover by directly calling `generation.abandon()`. They do not
prove a user can recover through the product.

### TG-S4-02 — No pre-dispatch failure boundary test

No test injects failure after `begin()` but before `generate_paid_clip()`.

### TG-S4-03 — Terminal replay tests compare only status

The suite tests success-to-failure, failure-to-success, and repeated identical
success. It does not test the same status with conflicting output, cost, or
error.

### TG-S4-04 — No concurrent production HTTP test

The repository has domain-level crash tests and job-manager tests, but no test
issues concurrent real HTTP compile requests and asserts one dispatch/provider
call. The adversarial reproduction confirmed the safeguard twice:

```text
first request:  200
second request: 409
compile calls:  1
```

### TG-S4-05 — No generation-specific background identity integration test

The suite protects generic background context capture but does not specifically
assert that a Director background job writes its generation ledger into the
captured project. The direct production-mechanism reproduction passed twice.

### TG-S4-06 — Structurally invalid ledger shapes are untested

Malformed JSON raises `LedgerUnreadable`. JSON-valid but structurally invalid
ledgers raise incidental `AttributeError` or `TypeError`. They still fail
closed, so this is not presently a spend defect, but the promised error contract
is incomplete.

## Rejected findings and safeguards confirmed

### R-S4-01 — `in_flight` is weaker than a request key for the audited crash window

**Not a finding.** For the same project, beat, shot, and signature, the HTTP job
key rejects concurrent compiles, the Director lock serializes other compiles,
`begin()` persists before provider dispatch, and the persisted `RUNNING` attempt
blocks replay after restart.

### R-S4-02 — Two paid calls can reach the provider for one shot

**Not reproduced.** Two production HTTP duplicate-request experiments each
returned HTTP 200 then 409 with one compile dispatch. Removing `in_flight` in a
mutation did produce two paid calls, proving the guard is load-bearing.

### R-S4-03 — Automatic jobs retry a stuck attempt

**Not a finding.** No automatic retry mechanism was found. Failed jobs become
`error`; another compile requires an explicit caller action.

### R-S4-04 — Corrupt lineage fails open

**Not a finding.** `load_attempts`, `for_shot`, `history`, `spend`, `begin`, all
`_finish` callers, `in_doubt`, and the Director paid path failed closed and
preserved corrupt bytes. The compile path was reproduced twice with zero
provider calls. Unrelated beats and projects remained usable.

### R-S4-05 — `in_doubt()` can mutate terminal lineage

**Not a finding.** Calling `in_doubt()` on a succeeded attempt returned the
existing result and left the ledger byte-for-byte unchanged.

### R-S4-06 — Conflicting-status terminal races corrupt the winner

**Not a finding.** One hundred `succeed`/`fail` races and 100
`succeed`/`abandon` races each produced one winner and one `TerminalConflict`.
The defect is limited to conflicting facts sharing the same terminal status.

### R-S4-07 — Background lineage follows the currently active project

**Not a finding.** Twice, a lineage job enqueued under Project A and released
after switching the process fallback to Project B wrote only
`A/generation/s1.json`.

## Mutation evidence

All four important safeguards were mutation-sensitive:

| Mutation | Result |
|---|---|
| Remove the `RUNNING`/signature `in_flight` guard | Crash replay failed; provider called twice |
| Treat ledger read/parse errors as empty history | Two fail-closed tests failed |
| Permit terminal attempts to be rewritten | Success/failure conflict tests failed |
| Discard captured background project context | Isolation test failed; job followed Project B |

## Suite evidence

Full suite against `main@ad0dab2`:

```text
286 passed
0 skipped
1 failed
287 collected
```

The sole failure was
`test_a_persisted_transition_survives_concurrent_readers`, caused by the
explicitly excluded Windows transient `os.replace` denial. It is not part of
the Slice 4 verdict and is documented as settled for Linux/Cloud Run.

## Final answer

The original Slice 4 double-spend, fail-open ledger, and conflicting-status
rewrite defects are remediated and protected by meaningful tests.

- The audited duplicate/restart window did not spend twice unless `in_flight`
  was deliberately removed.
- A paid failure can permanently strand a beat.
- A pre-provider failure can falsely strand a beat as possibly billed.
- Conflicting terminal facts can be silently discarded when they share the same
  terminal status.

Slice 4's financial safety is materially improved, but its failure recovery is
not yet a usable product workflow.

