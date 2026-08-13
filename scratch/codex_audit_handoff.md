# Codex Audit Handoff — current

Written by the implementation owner (Claude Code), 2026-08-12.
Supersedes `scratch/CODEX_AUDIT_RESTART_HANDOFF.md`, which describes the
2026-08-11 pre-remediation state and is kept only as a record.

## Role and posture

Codex is the independent adversarial auditor for FilmCraft V1; Claude Code is
the implementation owner. Findings by default, not patches. Preserve
reproducible evidence and mutation-test important safeguards.

Charter: `docs/FILMCRAFT_V1_CODEX_ADVERSARIAL_AUDIT_CHARTER.md`
Contract: `docs/FILMCRAFT_V1_CODE_IMPLEMENTATION_CONTRACT.md`

Both are now committed. They used to live only in a local Downloads
folder, so an auditor working from a clone -- or the author working from
another machine -- had neither the rules being audited against nor the
spec being audited. Copies in the repo travel with the code they govern.

## Current state

- Branch `main`, commit `8f9dd37`, pushed. Worktree clean apart from unrelated
  untracked scratch files.
- `python -m pytest tests/ -q` → **287 passed, 0 skipped**. A skip banner in
  `tests/conftest.py` reports any skips separately; a pass count alone is not
  evidence the suite ran.
- Slices 0–4 of the approved plan (`scratch/filmcraft_v1_plan.md`) are built.
  Slices 5–8 are not started.

## Audit history — all reports now committed

Read in order; each records findings, remediation and re-verification.

| Report | Scope | Status |
|---|---|---|
| `scratch/codex_adversarial_audit_pass_a.md` | Director contract | A-01…A-04 closed |
| `scratch/codex_adversarial_audit_pass_f.md` | Project isolation | F-01…F-03, PF-01 closed |
| `scratch/codex_adversarial_audit_slice_2.md` | Approval signature | S2-01…S2-03 closed |
| `scratch/codex_adversarial_audit_slice_4.md` | Generation lineage | S4-01…S4-03 remediated, **awaiting re-verification** |

The slice 2 and slice 4 reports were only committed at `8f9dd37`; an audit at
an earlier commit could not read them.

## Closed — do not re-litigate without new evidence

- A-04 warning identity: derived from content; a supplied id is kept as
  `source_id` and is never the disposition key.
- F-01 `save_current_project` writing to the process pointer; F-02 global job
  namespace; F-03 unscoped frontend reads; PF-01 reference registries.
- S2-01 delimiter-ambiguous signature preimage; S2-02 unpersisted transitions;
  S2-03 concurrent plan writes (incl. the Windows transient replace denial,
  closed at `8fda625` and independently verified over 19,200 saves).

## Explicitly out of scope

- Windows-only transient `os.replace` behaviour — settled.
- Cross-process locking and logical lost-update prevention — both documented in
  `backend/atomic.py` as *not provided*, by agreement.
- Slices 0–3 unless a later change touched them.

Prefer defects reachable in production on Linux/Cloud Run over workstation-only
conditions. An earlier round was spent on a Windows-only concurrency case that
cannot occur in the deployed environment.

## Next action: re-verify the Slice 4 remediation

Audit `d515320`..`8f9dd37`, against `scratch/codex_adversarial_audit_slice_4.md`.
Key files: `backend/generation.py`, the paid path in
`backend/director.py::_compile_locked`, `tests/test_generation_lineage.py`.

**Primary target — the S4-01 fix deliberately departs from the suggested fix,
and that reasoning is what most needs attacking.**

A durable request-level idempotency key threaded to every paid `begin()` was
tried and reverted: a key derived from the same inputs as the signature is
identical on a *legitimate* re-buy (media truncated or deleted) and refused to
replace footage that was genuinely gone — an existing test caught it. A random
key is a new key after the crash it must survive.

The claim now is that an **`in_flight`** guard subsumes request idempotency: if
an attempt for the same shot+signature is still `RUNNING`, `begin()` refuses and
the compile raises, because the provider may already have been billed. Recovery
is `generation.abandon()`, an explicit human act. Falsify that claim:

1. a duplicate or concurrent request `in_flight` does **not** catch but a
   request key would have;
2. any path where two paid calls reach the provider for one shot;
3. whether a beat can become permanently uncompilable — stuck `RUNNING` with no
   reachable recovery, since `abandon()` currently has **no API or UI**.

**Second — a behaviour change introduced by that fix.** Any exception from
`generate_paid_clip` now leaves the attempt `RUNNING` with the reason recorded
(`generation.in_doubt`) rather than failed, because once the provider has been
called the outcome is unknown. Consequence: ordinary paid failures require an
explicit `abandon` before retry. Attack whether that is safe, and whether
anything auto-retries into a stuck state.

**Third — S4-02 fail-closed reads.** `load_attempts` raises `LedgerUnreadable`
when a ledger exists but is unreadable or corrupt. Check every caller
(`for_shot`, `history`, `spend`, `begin`, `_finish`, `in_doubt`, the compile
path, any endpoint) fails closed without spending, and that one bad ledger
cannot break unrelated beats, projects or endpoints.

**Fourth — S4-03 terminal immutability.** Terminal attempts reject conflicting
transitions and treat identical ones as no-ops. Check `in_doubt()` cannot mutate
a terminal record, and look for races between concurrent
`succeed`/`fail`/`abandon`.

**Also in scope:** generation ledgers must obey per-request project identity
(`backend/projects.py`) — a background job must write its own project's lineage.

## Known gap, already disclosed

`generation.abandon()` has no API or UI. A real paid failure today strands the
beat until someone calls it from Python. Flagged when the change was made;
intended for the Slice 6 issue model. Worth confirming whether it is merely
awkward or actually unrecoverable.
