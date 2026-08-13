# FilmCraft V1 Adversarial Audit — Slice 4: Generation Lineage

Audit date: 2026-08-12  
Audited commit: `e2bdf2f` (`main`)  
Posture: findings only; no implementation patches

## Verdict

**Slice 4 is not safe to gate the next slice.** The lineage model and serial/concurrent happy-path tests are useful, but production does not use request idempotency, ledger read failures erase authoritative history, and terminal attempts can be rewritten. The no-double-spend and append-only guarantees are therefore falsified.

## Confirmed defects

### S4-01 — Production paid generation never supplies an idempotency key

- **Severity:** Critical
- **Confidence:** High
- **Contract violated:** §11.1 no double-spend across duplicate requests, worker restart, or network retry.
- **Cause:** `generation.begin()` implements `idempotency_key`, but the only production call in `director.compile_coverage()` omits it. No API or frontend path creates or forwards a generation request key. All idempotency-key coverage calls the storage primitive directly rather than the paid production wrapper.
- **Reproduction:** Called `begin()` for a paid shot/signature without a key, representing a process that reached the provider but died before `succeed()` or the plan marker. Replayed the same production-shaped call. Both returned `created`, produced distinct running attempts, and therefore permitted two paid calls.
- **Impact:** The exact crash window Slice 4 claims to cover still double-charges. Signature reuse cannot help because it only reuses `succeeded` attempts; the first attempt remains `running` when the process dies before recording success.
- **Suggested fix direction:** Mint or accept a durable request-level key before dispatch, persist it with the job/request, and pass it through `compile_coverage()` to every paid `begin()`. Define recovery for an old `running` attempt whose provider outcome is unknown; blindly creating another is not idempotent.
- **Required regression:** Exercise the real compile call path, interrupt after the simulated provider charge but before `generation.succeed()`, replay with the same durable request key, and assert the paid provider is called once.

### S4-02 — A ledger read error is treated as empty history and then overwrites it

- **Severity:** Critical
- **Confidence:** High
- **Contract violated:** §11.1 no double-spend and §11.6 append-only lineage.
- **Cause:** `load_attempts()` catches every `OSError` and returns `[]`, indistinguishable from a ledger that does not exist. `begin()` then appends to that empty list and atomically replaces the real ledger.
- **Reproduction:** Created and succeeded a paid attempt with a request key. Forced one transient `Path.read_text()` `OSError` for its ledger, then replayed `begin()` with the same key/signature. It returned `created`; after the write, the ledger contained only the new running attempt. The paid success was erased.
- **Impact:** A transient filesystem/GCS read fault both permits a second charge and destroys the record that could have prevented future charges. Atomic writes preserve the wrong replacement perfectly.
- **Suggested fix direction:** Distinguish missing from unreadable/corrupt. Only a confirmed nonexistent ledger may mean empty history. Read/parse failures must fail closed and must never be followed by a write derived from an empty substitute. Consider a recoverable error surfaced to the job/UI.
- **Required regression:** Inject `OSError` and malformed JSON into an existing paid ledger; assert `begin()` raises/refuses generation, the provider is not called, and the original bytes remain unchanged.

### S4-03 — Terminal attempts can be rewritten, erasing paid success and spend

- **Severity:** High
- **Confidence:** High
- **Contract violated:** §11.6 prior attempts never disappear; `GenerationAttempt` explicitly promises terminal records are never mutated.
- **Cause:** `_finish()` applies changes regardless of the current status. `succeed()` followed by `fail()` rewrites the same record rather than rejecting the late/conflicting transition.
- **Reproduction:** Began a paid attempt, marked it succeeded with `clip.mp4` and cost `0.60`, then called `fail()` for the same attempt. The stored record became `failed` while retaining output/cost; `spend()` reported zero paid attempts and zero spend.
- **Impact:** Duplicate/late callbacks or error handling can rewrite billing truth and failure history. The attempt list remains length one, but its authoritative terminal outcome is lost.
- **Suggested fix direction:** Permit transitions only from `running` to exactly one terminal state. Repeated identical completion may be idempotent; a conflicting terminal transition must be rejected and leave the stored record byte-for-byte unchanged.
- **Required regression:** Test success→failure and failure→success conflicts, plus repeated identical completions; assert the first terminal result wins and spend/history remain stable.

## Test gaps

- Idempotency tests cover `generation.begin()` directly, not the production paid-generation call path.
- No test exercises the charge-before-success-persistence crash window.
- No test distinguishes missing ledgers from unreadable or malformed ledgers.
- No test asserts terminal-state immutability.

## Safeguards confirmed

- Concurrent direct calls with the same explicit idempotency key create one attempt.
- A new key with identical inputs reuses an existing succeeded attempt when its output is non-empty and present.
- Changed signatures create a new branch; failed attempts remain visible during ordinary serial operation.
- Generation ledgers resolve through the bound project path.
- Shared atomic persistence provides unique temporary files, per-destination locking, fsync, and bounded Windows replace retry.

## Evidence

- Focused lineage/paid-rebill suite: **27 passed, 0 skipped**.
- Complete suite: **279 passed, 0 skipped**.
- S4-01 direct restart-window reproduction: two production-shaped calls, two `created` dispositions, two paid calls permitted.
- S4-02 injected read failure: prior paid success erased and replay returned `created`.
- S4-03 conflicting completion: succeeded attempt rewritten to failed; reported spend dropped from $0.60 to $0.
- No production implementation files were modified by Codex.
