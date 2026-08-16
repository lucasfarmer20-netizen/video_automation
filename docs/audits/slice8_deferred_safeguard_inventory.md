# Deferred-safeguard inventory — slices 0–4

Written for slice 8's mutation-sensitivity retrofit. Companion to
`orchestration_guardrails.md`, which is the doctrine this applies.

## Why this document exists

The roadmap says slice 8 exits when

> every safeguard added across slices 0–7 is mutation-sensitive.

Taken literally that is a codebase-wide retrofit. Slices 0–4 predate the
practice entirely: they were built before "a fix must ship a mutation that fails
a test" was a rule, and between them they added on the order of forty distinct
guards across eleven modules. Retrofitting all of them is an unbounded job, and
the guardrails exist to prevent exactly that shape — their own doctrine is
severity and reachability over exhaustiveness, and they say plainly that
counting findings never terminates.

So the round was scoped: cover the safeguards whose failure **costs money or
admits unapproved work**, and write down every other one, so that what is
deferred is *known* rather than silently skipped.

This document is that second half, and it is the part that makes the first half
honest. Without it, "we covered money and gates" is indistinguishable from "we
stopped when we got tired." A reader should be able to disagree with a deferral
here — the point is that there is something to disagree with.

## The rule that was applied

**In scope** — the five areas named in the round's brief:

| # | Area | Harness |
|---|---|---|
| 1 | `gate_cleared()` and Gate 1 | `scratch/mutate_gate1.py` (new) |
| 2 | spend accounting — `spend`/`billed`/`at_risk`/`unknown_spend` | `scratch/mutate_slice8_spend.py` |
| 3 | the paid path — `begin`/`succeed`/`fail`/`in_doubt`/`abandon`, the ledger schema gate, `record_paid_drafts` | `mutate_slice8_spend.py`, `mutate_slice8_tg_s4.py`, `scratch/mutate_paid_path.py` (new) |
| 4 | §11.7 export equivalence | `scratch/mutate_slice7.py` |
| 5 | the storage gate | `scratch/mutate_slice8_storage_gate*.py` |

**Promotion rule** — anything the inventory turned up that guards an
*irreversible* outcome which is neither money nor a gate (data loss, an
overwrite, a destroyed record) is pulled into scope rather than deferred. Three
items met that test; they are in the next section.

**Everything else** is listed below with three facts: what it guards, what
failing silently would cost, and whether it is reachable in the deployed
environment.

### What "reachable" means here

Deployed is Cloud Run: Linux, one container, Firestore as the durable store, a
GCS mount for media, the FastAPI app in `backend/main.py` as the only server.
Against that:

* **`pipeline.py` is not reachable deployed.** It is the workstation CLI. Its
  Gate 1 re-read is still covered (`mutate_gate1.py`), because CLAUDE.md names it
  as the only entrypoint and a local run can spend real money — but a defect
  confined to it is a workstation defect.
* **The frontend is reachable**, and its failures are visible rather than silent,
  which is why they rank lower than a server-side one of the same shape.
* **Windows-only conditions are out of scope by policy** (`orchestration_guardrails.md`,
  `out_of_scope: workstation_only_conditions`). POSIX `rename` does not fail the
  way `os.replace` does on Windows. Items below marked *workstation-only* are
  recorded and must not be looped on.

## Promoted out of the deferred list

Three items guard outcomes that cannot be undone by retrying. Money can be
re-spent; a storyboard overwritten by another film's cannot be recovered from
anywhere, because the JSON mirror and the Firestore document are both gone in
the same operation.

| ID | Safeguard | Why promoted |
|---|---|---|
| **P1** | `save_current_project` writes to the **bound context**, never the active-project pointer (`backend/main.py`) | This is Audit Pass F's finding, and it is the worst shape in the codebase: working on project B overwrote project A's *whole manifest* — every approval, selection and timing — and reported success. `manifest.save`'s default was already bound-aware; its main caller passed the active path explicitly and overrode it, so hardening the primitive was not enough. Not money: an irreversible destruction of approved work. |
| **P2** | An unknown `X-Project-Id` is **refused**, not silently served the active project (`bind_project_context`, `backend/main.py`) | The read case is a display defect. The write case is P1 by another route: a client naming a project the server cannot resolve, answered about whichever film happens to be active, writes into it. |
| **P3** | `_move_tree` **keeps the source** when the copy is short (`backend/main.py`) | Project delete/move on a gcsfuse mount falls back to copy-then-delete. A half-finished copy that reported success and then deleted the original destroys a film. The guard is a byte-count comparison and a `RuntimeError`; nothing was mutation-testing it. |

These are covered by `scratch/mutate_isolation.py`. Their reachability is
deployed-and-routine, not exotic: P1 fires whenever two projects are open in one
browser session, P2 whenever a client's project switch races a request, and P3
on every project delete, because the GCS mount is the normal deployed
filesystem.

### Considered for promotion and not promoted

**Slice 3's "generated media survives a rejected compile."** The behaviour is
real and tested (`tests/test_script_invalidation.py::test_generated_media_survives_a_script_change`),
and destroying bought clips would be irreversible. It is *not* promoted because
there is no line to remove: the safeguard is the absence of a deletion — the
staleness check refuses before anything touches media. A faithful mutation would
have to *insert* a delete, which measures whether the test would notice a defect
nobody has written rather than whether an existing guard is pinned. That is a
worthwhile test-design exercise and a poor mutation. Recorded here as D3-3 so it
is a decision rather than an omission.

## The inventory

Severity is the guardrails' scale: **High** = reachable and expensive or
irreversible; **Medium** = reachable, recoverable, misleads a human; **Low** =
cosmetic, or not reachable deployed.

### Slice 0 — the six-stage spine (`backend/stages.py`)

| ID | Safeguard | What it guards | Cost of failing silently | Reachable deployed | Tests today | Sev |
|---|---|---|---|---|---|---|
| D0-1 | `stages.py` is the **single server-side authority** for stage, blocked reason and primary action; `StageHeader` renders it verbatim | A browser computing its own idea of "approved" and disagreeing with the server about whether money may be spent | The studio offers a paid action the server will refuse, or shows a beat as ready when it is not. It cannot itself spend — every paid route re-checks, and those checks *are* pinned — so the cost is misdirection, not money | Yes, every page load | `tests/test_stages.py` | Medium |
| D0-2 | Refine never reports "complete" | Claiming a review nobody performed | A human ships believing an optional polish pass was done | Yes | `tests/test_stages.py` | Low |
| D0-3 | Panels routed to the stage that owns them | A control appearing where its gate does not apply | Confusion; no gate is bypassed | Yes | `tests/test_stages.py` | Low |
| D0-4 | The narrator is a **project setting** resolved through `manifest.narrator_name()`, persisted in the manifest rather than a config global | An episode's designed voice surviving a cold start | This one touches money. `/api/voice/settings` used to assign a module global, so a designed Vesper voice was lost on the next cold start and narration fell back to the stock default — a whole episode re-narrated at ElevenLabs, or shipped in the wrong voice | Yes, and cold starts are routine on Cloud Run — this is how the defect was found | `tests/test_manifest_roundtrip.py`, casting tests | **Medium-High** |

**D0-4 is the closest deferred item to the money line and is the recommendation
if the scope widens by one.** It is deferred here only because the charge is a
narration re-render rather than a Tier-C clip, and because the failure is loud
(the wrong voice is audible) rather than silent.

### Slice 1 — identity that travels with the work

Covered already: enqueue-time context capture and worker rebind, and the ledger
path following the bound project, are both pinned by
`scratch/mutate_slice8_tg_s4.py` (TG-S4-05). Create/delete identity is pinned by
`scratch/mutate_slice8.py` (#7, #8). Promoted this round: P1, P2, P3.

| ID | Safeguard | What it guards | Cost of failing silently | Reachable deployed | Tests today | Sev |
|---|---|---|---|---|---|---|
| D1-1 | Every response carries `X-Project-Id` | A client that has switched discarding replies that lost the race | The studio renders another film's data under the current film's heading. Read-only — the write path is P1/P2 | Yes | `test_every_response_names_the_project_it_is_about` | Medium |
| D1-2 | The job registry is keyed by **(project_id, name)**, not name alone | Two films running the same stage; one shown the other's progress as its own | Under the old global registry, one film's render blocked every other film's project switch, and job logs were shared. Not an overwrite: jobs carry their own context now, which is the part that *is* pinned | Yes | `test_two_projects_can_run_the_same_stage_at_once`, `test_a_project_only_sees_its_own_jobs`, `test_same_named_jobs_do_not_share_a_log_buffer` | Medium |
| D1-3 | A project switch is refused while **this project's** job is running | A running job's output being redirected mid-flight | Explicitly belt-and-braces: the real defence is the enqueue-time capture, and that is pinned. Its own docstring says so | Yes | `test_project_switch_is_refused_while_a_job_runs`, `test_the_switch_guard_reads_the_real_job_registry` | Low |
| D1-4 | `ProjectContext` is immutable and every derived path stays inside its own project root | Path traversal out of a project directory | A read or write outside the project tree. The containment check on `/api/project/select` is the reachable edge of this | Yes | `test_every_derived_path_stays_inside_its_own_project`, `test_a_context_is_immutable` | **High — see note** |
| D1-5 | A binding is undone when its block exits; concurrent threads do not share one | A leaked binding making the next request answer about the wrong film | Same class as P1, one layer down. Deferred because the leak is caught by the promoted P1 mutation at the point where it would do damage | Yes | `test_a_binding_is_undone_when_the_block_exits`, `test_concurrent_threads_do_not_share_a_binding` | Medium |

**D1-4 is High and is deferred with reservations.** Containment is what stops a
project path resolving outside the workspace root, and the operations behind it
include delete. It is not promoted this round only because the three promoted
items exercise the same `main.py` write-target machinery and a fourth would not
change the round's conclusion. It should be first in the next one.

### Slice 2 — approval bound to the exact plan it was given for

| ID | Safeguard | What it guards | Cost of failing silently | Reachable deployed | Tests today | Sev |
|---|---|---|---|---|---|---|
| D2-1 | `plan_signature()` builds a **canonical JSON preimage** keyed by field name, carrying `SIGNATURE_VERSION` | One plan's approval covering a materially different plan | S2-01: the old preimage joined stringified fields with `\|`, so `purpose="alpha\|beta"` / `subject="gamma"` and `purpose="alpha"` / `subject="beta\|gamma"` hashed identically. Those are user-controlled strings — reachable **by typing** — and the consequence is paid generation of a plan nobody approved | Yes | `tests/test_plan_approval.py` | **High** |
| D2-2 | `load_plan` detects drift in **one place**, drops the stale approval, records it, and **persists** the transition once | The file going on asserting an approval that no longer exists | S2-02: the invalidation happened in memory only, so anything not reading through `load_plan` saw a false approval, and the history was rebuilt on every read | Yes | `tests/test_plan_approval.py` | **High** |
| D2-3 | `_NON_MATERIAL_*_FIELDS` completeness: a test asserts the material and non-material sets account for the whole dataclass | A field added to `DirectorShot`/`CoveragePlan` silently falling outside the signature | Approval drift again, arriving through a future change rather than a present bug. This is a meta-guard and the most durable of the three | Yes | `tests/test_plan_approval.py` | Medium |
| D2-4 | `save_plan` is atomic | A torn plan file | A lost plan, not a stale one — irreversible, but the atomic primitive underneath it *is* exercised by `tests/test_atomic_reads.py` and by every ledger write the covered harnesses drive | Yes | `tests/test_atomic_reads.py` | Medium |
| D2-5 | `atomic.py`: unique temp name per writer, per-destination lock, bounded replace retry | Two writers to one beat colliding on a shared temp name | The unique temp name matters on Linux. The **replace retry** is workstation-only: it exists for a Windows `os.replace` denial that cannot occur on Cloud Run | Partly — the temp name yes, the retry no | `tests/test_atomic_reads.py` | Medium / Low (retry) |
| D2-6 | `read_json` takes the same per-destination lock as the writer (`99a883b`) | A reader denied by a concurrent replace, reporting a present record as unreadable | **Workstation-only.** The commit says so and fixed it anyway, because a permanently red suite trains people to ignore red. Recorded as Low by policy; do not spend a round on it | No | `tests/test_atomic_reads.py` | Low |

**D2-1 and D2-2 meet the round's own in-scope criterion** — their failure admits
unapproved work and costs money — and are not in the enumerated five. They were
not silently deferred: they are named here as the top of the list, and the
enumeration is what should change, not the criterion. Recommended as the whole
content of the next retrofit round.

### Slice 3 — catching the script change nothing was looking for

| ID | Safeguard | What it guards | Cost of failing silently | Reachable deployed | Tests today | Sev |
|---|---|---|---|---|---|---|
| D3-1 | `beat_signature()` covers narration text **and** duration together; a plan records the beat it was locked against | A line rewritten to the same length passing every other check | Every prompt in the plan still describes the old line, and it compiles without comment — so paid clips are generated against narration that no longer exists, and the fix is to buy them again | Yes | `tests/test_script_invalidation.py` | **High** |
| D3-2 | Whitespace is normalised out of the signature | Reflowing a line reading as a script change | A warning that fires on reformatting is a warning that gets clicked through — the guard is destroyed by being too sensitive, which is harder to notice than it being absent | Yes | `test_whitespace_only_edits_are_not_a_rewrite` | Medium |
| D3-3 | A rejected compile leaves generated media untouched | Staleness deleting work rather than blocking it | Destruction of bought clips. Considered for promotion; not promoted because the safeguard is the *absence* of a deletion and a faithful mutation would have to insert one — see above | Yes | `test_generated_media_survives_a_script_change` | **High**, but not mutable as written |
| D3-4 | A script change does not touch unrelated beats | A rule that marks the whole episode stale over one line | Every beat re-locked, so the gate is trained out of the user in a day | Yes | `test_a_script_change_does_not_touch_unrelated_beats` | Medium |
| D3-5 | A plan with **no recorded baseline** reports NOT stale | Flagging every legacy plan on the first load after deploy | The over-flagging direction: the original narration is not recoverable, so calling it stale would be a guess, and every pre-slice-3 plan would demand a re-lock at once | Yes, once, on the first deployed load | `test_a_plan_with_no_baseline_is_not_called_stale` | Medium |

**D3-1 meets the in-scope criterion too**, on the same footing as D2-1/D2-2.

### Slice 4 — generation lineage

Largely covered. `mutate_slice8_spend.py` pins the reporting side,
`mutate_slice8_tg_s4.py` the ledger schema gate and identity, and
`scratch/mutate_paid_path.py` (new this round) pins the permission side:
`begin()`'s four dispositions, the dispatch boundary (S4-R02), terminal facts
(S4-R03) and what counts as usable media. What remains:

| ID | Safeguard | What it guards | Cost of failing silently | Reachable deployed | Tests today | Sev |
|---|---|---|---|---|---|---|
| D4-1 | Attempt ids are **URL-safe** | The recovery route being reachable at all | The original `shot#n:uuid` truncated at the `#` — the browser treated the rest as a fragment, the abandon endpoint answered 405, and an ordinary provider timeout made an approved beat permanently uncompilable for anyone without a Python shell. Not a spend, a **hidden manual pipeline step**, which the contract forbids | Yes | `tests/test_generation_lineage.py` recovery tests | Medium |
| D4-2 | A retry **branches** from the most recent attempt (`parent_attempt`) | Lineage becoming a flat list | The record stops saying what was tried before. Diagnostic, not billing | Yes | `test_a_retry_branches_from_the_attempt_it_retries` | Low |
| D4-3 | A failed attempt stays **attached to its shot** (§6) | Deleting the failure | The next person sees a shot with no media and no reason why | Yes | `test_a_failed_attempt_stays_attached_to_its_shot` | Medium |
| D4-4 | `abandon()` **requires a stated reason** | An unexplained write-off of a possible charge | Abandoning may be writing off money that was actually spent; without the reason the record cannot say who decided that or why | Yes | `test_abandoning_requires_a_stated_reason` | Medium |

### H1 — manifest loader hardening (`4eeb976`, between slices 4 and 5)

Included because it sits inside the slice 0–4 window and one of its guards is on
the money line.

| ID | Safeguard | What it guards | Cost of failing silently | Reachable deployed | Tests today | Sev |
|---|---|---|---|---|---|---|
| H1-1 | A missing or unrecognised `motion_type` defaults to **parallax**, never into the paid tier | A beat with no tier recorded defaulting into Tier C | Unplanned spend, on every beat a newer writer or a hand-edit leaves untiered. **This one is money, and it is now covered** — `scratch/mutate_paid_path.py` pins both the missing-key default and the unrecognised-value fallback | Yes | `test_missing_motion_type_defaults_to_parallax` | **High — covered** |
| H1-2 | `from_dict` **drops** unknown keys rather than raising | One stray field costing a whole storyboard | `Shot(**shot)` used to explode on any extra field, and a `from_dict` that raises takes the project offline. What keeps that from becoming permanent is `get_current_project` refusing to overwrite a manifest it cannot read | Yes — a newer writer adding a field is the ordinary case | `tests/test_manifest.py` | **High** |
| H1-3 | A non-dict `camera` falls back to a default rather than raising | Same class, one level down | Same | Yes | `test_non_dict_camera_becomes_a_default_camera` | Medium |

**H1-2 is High and deferred.** It guards availability rather than money, and the
refusal-to-overwrite that backs it is the same `get_current_project` path the
storage-gate harness already drives — but from the storage axis, not this one.
Second on the list for the next round, after D2-1/D2-2.

## Observed while surveying, outside slices 0–4

Recorded rather than acted on, because attributing them to a slice would be a
guess and neither is this round's scope:

* `manifest.save_project` **deletes** Firestore beat documents absent from what
  it writes (the zombie-beat reconciliation). It is the only destructive write
  in the durable store, its guard is one `keep` set, and a defect in it is
  irreversible. It is partly exercised by the storage-gate harness's
  partial-read mutation — which exists precisely because a truncated read plus
  this delete makes the truncation durable — but the `keep` set itself has no
  mutation.
* `require_studio_key` gates non-GET requests only, so every refusal body on a
  GET is world-readable. The storage gate's redaction mutation pins that one
  case; nothing pins the general rule.

## What would close the rest

In the order a next round should take them, by severity and reachability:

1. **D2-1, D2-2, D3-1** — approval drift and script drift. All three admit
   unapproved paid work, all three are reachable by typing, and all three meet
   this round's own in-scope criterion. One harness could cover them: they share
   `director.py`'s signature machinery.
2. **D1-4, H1-2** — containment, and the loader that keeps a project reachable.
   Availability and path safety rather than spend.
3. **D0-4** — the narrator setting, the last item that touches a bill.
4. Everything else in this document is Medium or below, and per the guardrails a
   round that yields no reachable High is a round that should not have run.

Nothing here is a defect. Every safeguard listed is present and tested; what is
absent is a mutation proving the test would notice its removal. That distinction
is the whole subject of this document, and it is why none of this was filed as a
finding.
