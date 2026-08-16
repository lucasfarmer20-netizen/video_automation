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
| 6 | the Director approval boundary (Audit Pass A) | `scratch/mutate_pass_a.py` (new) |

Row 6 was not in the brief's five, and it is here because **the first version of
this document left it out entirely** — not deferred with a reason, absent from
both lists. It was found by re-reading the inventory against `b1e466e` after the
document was written. It is recorded rather than quietly added because an
inventory's failure mode is omission, not error, and a document whose own
omission is invisible teaches the wrong lesson about how much to trust it.

It is not deferrable. The scope bar is "costs money or admits unapproved work",
and this is the second clause exactly: `force=true` used to send an unapproved
plan into a compile that generates stills and buys paid video, and the
`unresolved_warnings` check on the compile route (`backend/main.py`) sits
directly on the paid dispatch path. On this document's own ranking it belongs
*above* D2-1/D2-2 — those let a stale approval through, this one lets work
through with no approval at all.

**Promotion rule** — anything the inventory turned up that guards an
*irreversible* outcome which is neither money nor a gate (data loss, an
overwrite, a destroyed record) is pulled into scope rather than deferred. It is
unconditional: "something similar is already covered" is not a reason to decline,
because similar coverage is not equivalent mutation coverage. Four items met the
test; they are in the next section.

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
* **Firestore has delete protection and point-in-time recovery, 7-day
  retention.** This changes how severe several items below are, so it is stated
  once here rather than assumed: deployed, a destroyed *manifest* is restorable
  for a week. It does not cover media — that lives on the GCS mount — and it
  does not apply locally, where `db is None` and the JSON file is the only
  store. Where a rating below depends on it, it says so.

## Promoted out of the deferred list

Four items guard outcomes that retrying cannot undo. Money can be re-spent; the
work these protect cannot be re-made by asking again. Three were promoted when
this document was written; **P4 was promoted after review**, having first been
deferred for a reason that was about the round rather than about the safeguard.

**"Irreversible" needs stating precisely, though, because it is the word a
reviewer should press on and the three do not all earn it the same way.**

* **P3 is unrecoverable, full stop.** Generated media lives on the GCS mount and
  nowhere else — no database holds a copy — so a file dropped by a short copy
  and then `rmtree`d exists nowhere afterwards.
* **P1 and P2 are recoverable, and the cost of that recovery is the actual
  reason they are promoted.** The overwrite destroys the JSON manifest.
  Deployed, that is a *mirror*: `save_current_project` stamps `sb.id` from the
  bound context and writes Firestore first, so under the Pass F defect project
  A's durable document survives and only A's JSON copy is clobbered. Firestore
  on this project has delete protection and point-in-time recovery enabled with
  a **7-day retention window**, so deployed the manifest is restorable for a
  week.

  What makes it a promotion anyway is what that recovery requires. Nothing
  surfaces the loss: the studio answers 200, the save "succeeds", and the film
  on screen is simply the wrong one's. The only remedy is a database
  point-in-time restore that **the user does not know to ask for**, and the
  seven-day clock starts without anyone noticing it has started. A silent
  destruction whose repair depends on someone spotting it within a week is the
  right category, even though the bytes are not immediately gone.

  Two cases where it *is* total, and both are ordinary rather than exotic:
  locally there is no Firestore at all — `db is None`, the JSON file is the
  store of record, and this is the shape `mutate_isolation.py`'s probe measures;
  and deployed, the clobbered mirror is what the next bootstrap copies **up**,
  which the storage-gate work already identified as a live path. Add the id
  stamp going wrong at the same time (P1c, which is why that mutation is in the
  harness) and the durable document is overwritten directly.

P2 inherits P1's analysis exactly, because its write case reduces to P1 — and
that write case is the whole of its justification, which is worth stating
plainly because the first version of its harness did not cover it. The probe and
the tests sent only GETs, so a middleware that refused reads and fell back on
writes passed everything. `/api/approve` is the sharpest demonstration of what
that permits: it sets `approved` on every beat and `storyboard_approved` on the
episode, so a POST naming a project the server cannot resolve does not merely
write into the active film, it **clears Gate 1** on one nobody named. Covered
now, by a write probe and a test that assert byte-for-byte preservation of the
active project as well as the refusal.

P4 needs none of the PITR qualification above: Firestore holds manifests, and
what `purge` removes is a directory of generated media.

| ID | Safeguard | Why promoted |
|---|---|---|
| **P1** | `save_current_project` writes to the **bound context**, never the active-project pointer (`backend/main.py`) | This is Audit Pass F's finding, and it is the worst shape in the codebase: working on project B overwrote project A's *whole manifest* — every approval, selection and timing — and reported success. `manifest.save`'s default was already bound-aware; its main caller passed the active path explicitly and overrode it, so hardening the primitive was not enough. Not money: a silent destruction of approved work, repairable only by a restore nobody knows to ask for — see the analysis above. |
| **P2** | An unknown `X-Project-Id` is **refused**, not silently served the active project (`bind_project_context`, `backend/main.py`) | The read case is a display defect. The write case is P1 by another route: a client naming a project the server cannot resolve, answered about whichever film happens to be active, writes into it. |
| **P4** | `/api/project/delete` refuses a path **outside the workspace root** (`backend/main.py`) | Promoted after review, having first been deferred for a bad reason (see D1-4). The handler takes a caller-supplied path, and `purge: true` is a real `shutil.rmtree` rather than a move to `_trash` — so nothing recovers it: no Firestore copy, no trash directory, no PITR, because media and project directories are not in the database. The containment check is the only guard between that path and the unlink, and it had no mutation and no test. |
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
`scratch/mutate_slice8.py` (#7, #8). Audit Pass A's Director approval boundary,
which landed in the same commit as slices 0 and 1, is pinned by
`scratch/mutate_pass_a.py` — see the note on row 6 above for why it is covered
rather than listed here. Promoted this round: P1, P2, P3.

| ID | Safeguard | What it guards | Cost of failing silently | Reachable deployed | Tests today | Sev |
|---|---|---|---|---|---|---|
| D1-1 | Every response carries `X-Project-Id` | A client that has switched discarding replies that lost the race | The studio renders another film's data under the current film's heading. Read-only — the write path is P1/P2 | Yes | `test_every_response_names_the_project_it_is_about` | Medium |
| D1-2 | The job registry is keyed by **(project_id, name)**, not name alone | Two films running the same stage; one shown the other's progress as its own | Under the old global registry, one film's render blocked every other film's project switch, and job logs were shared. Not an overwrite: jobs carry their own context now, which is the part that *is* pinned | Yes | `test_two_projects_can_run_the_same_stage_at_once`, `test_a_project_only_sees_its_own_jobs`, `test_same_named_jobs_do_not_share_a_log_buffer` | Medium |
| D1-3 | A project switch is refused while **this project's** job is running | A running job's output being redirected mid-flight | Explicitly belt-and-braces: the real defence is the enqueue-time capture, and that is pinned. Its own docstring says so | Yes | `test_project_switch_is_refused_while_a_job_runs`, `test_the_switch_guard_reads_the_real_job_registry` | Low |
| D1-4 | `ProjectContext` is immutable and every derived path stays inside its own project root | A captured context changing after capture, and derived paths leaving the project tree | The immutability half is guarded by P1's mutations, which write through a bound context. The **workspace containment** half was promoted to P4 — see above | Yes | `test_every_derived_path_stays_inside_its_own_project`, `test_a_context_is_immutable` | Medium (containment split out) |
| D1-5 | A binding is undone when its block exits; concurrent threads do not share one | A leaked binding making the next request answer about the wrong film | Same class as P1, one layer down. Deferred because the leak is caught by the promoted P1 mutation at the point where it would do damage | Yes | `test_a_binding_is_undone_when_the_block_exits`, `test_concurrent_threads_do_not_share_a_binding` | Medium |

**D1-4's containment half was promoted (P4), and the reason it was originally
deferred does not survive scrutiny.** The first version of this document declined
to promote it "only because the three promoted items exercise the same `main.py`
write-target machinery and a fourth would not change the round's conclusion."
That is a statement about the round, not about the safeguard. The promotion rule
is unconditional on irreversibility, and similar coverage is not equivalent
mutation coverage — P1, P2 and P3 mutate three different guards and none of them
touches the path check on `/api/project/delete`. Corrected after review.

What remains under D1-4 is the immutability of `ProjectContext` and the derived
paths hanging off `root`. Those are Medium: a frozen dataclass and property
accessors, exercised indirectly by every P1 mutation, with no separate way to
fail silently.

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

## Open finding: approval is never type-checked (NOT fixed here)

Found while closing a review finding about Gate 1, and left alone deliberately,
because it is a **production defect rather than an unpinned safeguard** and this
was a test-and-tooling round.

`Storyboard.from_dict` filters unknown keys but does not coerce types, so a
manifest's `approved` value reaches `gate_cleared()` exactly as written. The
predicate is a truthiness test, which handles `null`, `0` and a missing key
correctly — all falsy, gate shut, and all three are now pinned by
`test_an_approval_that_is_not_true_never_clears_the_gate`.

What it does not handle is a truthy non-boolean. A beat carrying
`"approved": "no"` is approved as far as Gate 1 is concerned, and a paid Tier-C
beat with that value clears the gate **with no mutation at all**:

```
PROBE_GATE_STRING_APPROVAL=True      # scratch/mutate_gate1.py, pristine
```

Contract §5.4 requires approval to be explicit before paid generation, and a
string is not an explicit approval. The fix is coercion or validation at the
loader, which is new behaviour and therefore a build round — the guardrails
forbid it in a fix round, and no test here asserts the current answer is
desirable. Reachable by a hand-edited manifest or a writer of a different
vintage; not reachable through the studio, which writes booleans.

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
2. **H1-2** — the loader that keeps a project reachable. Availability rather
   than spend. (Containment left this list when it became P4.)
3. **D0-4** — the narrator setting, the last item that touches a bill.
4. Everything else in this document is Medium or below, and per the guardrails a
   round that yields no reachable High is a round that should not have run.

Nothing here is a defect. Every safeguard listed is present and tested; what is
absent is a mutation proving the test would notice its removal. That distinction
is the whole subject of this document, and it is why none of this was filed as a
finding.

## How to check this document, and how it already failed once

The failure mode of an inventory is **omission**, not error. Every entry above
can be argued with, which is the point; what cannot be argued with is a
safeguard that appears in neither list, because there is nothing on the page to
disagree about. The first version of this document did exactly that with Audit
Pass A — five guards on the Director approval boundary, one of them on the paid
dispatch path — and it was found only by re-reading the document against the
commits rather than against the code.

So the check that matters is not "is each rating right". It is:

1. Take the slice 0–4 commits (`b1e466e`, `a419310`, `2a3b0de`, `7352a71`,
   `e2bdf2f`, `d515320`, `3d2885d`, plus `4eeb976` and `99a883b`) and read what
   each commit message says it added.
2. For each guard named there, find it in the covered table or the deferred
   list.
3. Anything in neither is this document's real error.

Note the trap in step 1, because it is what hid Pass A: `b1e466e` carries slices
0 *and* 1 *and* two audit passes. Reading it as "the slice 0/1 commit" is how
four closed defects in the same diff go uncounted.
