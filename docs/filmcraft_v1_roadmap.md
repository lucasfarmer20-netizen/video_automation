# FilmCraft V1 — roadmap to done

For the orchestrator and both agents. Written 2026-08-13, `main` @ `6fcf23d`,
suite 324 passed / 0 skipped.

Read alongside:

- `docs/FILMCRAFT_V1_CODE_IMPLEMENTATION_CONTRACT.md` — what is being built
- `docs/FILMCRAFT_V1_CODEX_ADVERSARIAL_AUDIT_CHARTER.md` — how it is audited
- `docs/audits/orchestration_guardrails.md` — **when to stop** (governing)
- `scratch/filmcraft_v1_plan.md` — the original slice plan
- `scratch/codex_audit_handoff.md` — closed and out-of-scope lists

**Done is not slice 8.** Done is contract §15: fourteen things a *user* can do
end-to-end on a deployed instance. A green suite is necessary and not
sufficient, and nothing is deployed today — Cloud Run still runs `a8003ca`.

## Where things stand

| Slice | State |
|---|---|
| 0 Stage model | built, audited |
| 1 Project identity | built, audited (Pass F, cleared by Pass G) |
| 2 Plan signature + approval | built, audited (three rounds) |
| 3 Script → Director invalidation | built |
| 4 Generation lineage | built, audited (two rounds) |
| 5 Slot-based timeline | backend + API + tests built; **frontend not bound** |
| 6 Refine routing | not started |
| 7 Frozen export | not started |
| 8 Hardening | not started, and cannot be specified yet |

## Human gates

Three points where the decision is the owner's, not inferrable from the
contract. Between them the build/audit loop runs unattended under the
guardrails.

1. **After 5b** — the guardrails' first real exercise. Check they fired
   sensibly (did a round stop on severity, or run to the cap?) before trusting
   them on larger surface.
2. **Before slice 7** — the export snapshot design. §11.7 equivalence is the
   invariant most expensive to get wrong and hardest to retrofit, and the
   contract constrains the shape without determining it. Decide it, do not
   discover it.
3. **At §15** — the end-to-end walkthrough on a deployed instance. This is the
   completion criterion; no suite result substitutes for it.

---

## H1 — manifest loader hardening

First, because it is two functions in one file, the failing tests already exist
and are `strict=True`, so the exit criterion enforces itself. The guardrails
have never been exercised; run them here.

**Scope:** `backend/manifest.py`, `from_dict()` only.

**Defects** (pre-existing production code, not FilmCraft debt):

1. `sfx_layers` has no shape check and fails two ways. A truthy non-mapping
   layer raises `AttributeError` and costs the whole storyboard; a falsy one is
   absorbed by `(lay or {})` into a phantom default `AudioLayer()`, which is
   truthy and so silently suppresses the legacy ambience fallback in
   `resolve_sfx_layers`. **Both halves together** — fixing only the raise leaves
   the silent case live, which is the worse of the two.
2. An unrecognised `motion_type` raises instead of falling back to `parallax`,
   so one bad value costs the whole storyboard.

`camera` already guards with `isinstance`; the fix is to make `from_dict`
consistent with what it already does.

**Exit:** the three strict xfails XPASS and their markers are removed (strict
means the suite fails until they go); a malformed field is dropped with a
printed note, never fatal and never silently defaulted into something truthy;
no change to `gate_cleared()` or `load`/`save` semantics, which were just
pinned.

---

## 5b — bind the timeline UI to slots

**Scope:** `MultitrackTimeline.tsx` and whatever in `page.tsx` feeds it, reading
`GET /api/timeline/slots` instead of `project.shots`.

**Exit — these clauses hold, and are what the audit tests:**

- **§7.1** a timeline clip is a slot tied to a DirectorShot, not a file path;
  selecting a different take replaces media in the existing slot without
  destroying slot identity, valid trims, or placement
- **§6.2** incomplete coverage is stated ("3/5 visuals ready · Draft 1 will use
  2 placeholders") and Draft 1 is buildable from that state
- **C5** a placeholder in the UI carries `shot_id`, slot identity, intended
  duration, expected media type and source beat — not a grey rectangle
- **§11.4** no UI state claims a slot is filled unless the server says so

**Constraints:** `frontend/AGENTS.md` — this Next.js differs from training data;
read the relevant guide in `node_modules/next/dist/docs/` before writing code.
No production API changes: the slot contract is settled, and changing it here
reopens slice 5.

---

## 6 — Refine routing

**Scope:** new `backend/issues.py`, `ProblemQueueDrawer.tsx`.

**Unsettled design, to be decided in the slice** (the contract constrains but
does not determine): what an issue *is* as a record, how severity and
blocking are represented, and how "the authoritative stage for this fix" is
encoded so routing is data rather than a chain of conditionals.

**Exit:**

- **§8** issues attach to an exact shot, timeline position, audio event, scene
  or grade region; each carries severity, blocking vs non-blocking, a
  diagnosis, the smallest suggested fix, and the stage responsible
- **§8** routing holds: media/take → Generate, timing/edit → Rough Cut or
  Refine, intent/type/duration → Director
- **§8** optional polish does not block export; blocking issues may
- **§2.2** no screen mutates another stage's authoritative state

**Carry into this slice:** a stuck paid attempt is an issue, and Refine is where
it belongs. `generation.abandon()` has an API but no UI; a beat stranded by an
uncertain provider outcome should appear here with its reason and route to
Generate. That closes the last of S4-R01.

---

## 7 — Frozen export

**The highest-stakes slice.** A defect here ships a deliverable that does not
match what was reviewed, and the contract calls the invariant critical.

**Decide before starting** (human gate 2): the snapshot format and location,
what exactly is frozen, whether FCPXML is generated from the snapshot at export
time or stored beside the master, and how export history is kept.

**Scope:** `bundle.py`, `timeline.py`, `main.py`, `exports/<version>/`.

**Exit:**

- **§9.1** every export binds to an immutable snapshot identifying the project
  version, script/timing state, Director plan version, approved shot state,
  selected outputs, timeline state, audio state and grade state
- **§9.1** later edits create a new version; a prior master is never overwritten
- **§11.7** the rendered master and the FCPXML describe the **same** frozen
  timeline state — not the same live one, and not one refreshed between them
- **§9.2** export history retains version, type, preset, timestamp, status and
  snapshot identifier
- **Migration (approved constraint):** historical exports with no snapshot
  provenance are marked legacy/unverifiable. **Never fabricate a snapshot for
  them** — that would assert an equivalence nobody recorded.

**Audit posture:** full adversarial pass, not a light one. The charter's Pass H
and Pass I exist for this.

---

## 8 — Hardening

Cannot be fully specified until 5b, 6 and 7 land; its content is what they turn
up, plus the standing backlog:

- an abandoned-then-succeeded attempt under-reports spend (`generation.spend()`
  counts only paid+succeeded, so a clip bought and used reports $0)
- TG-S4-04 concurrent HTTP compile
- TG-S4-05 background-job generation identity
- TG-S4-06 structurally invalid ledger shapes (JSON-valid, wrong shape)
- known workstation-only flake: `test_a_persisted_transition_survives_concurrent_readers`
  on the Windows `os.replace` denial. Out of scope for production; do not spend
  a round on it.

**Exit:** contract §13's test categories are covered by behavioural tests, and
every safeguard added across slices 0–7 is mutation-sensitive.

---

## §15 — the actual definition of done

A user can, on a deployed instance: open or create a script; confirm narration
timing; enter Director; review multi-shot coverage; review Critic feedback;
approve coverage before spend; generate approved media with visible lineage;
survive failures and retries without losing intent or double-spending; build
Draft 1 with placeholders; watch and edit the Rough Cut; refine targeted issues;
export an immutable final master; export a matching FCPXML; and reproduce which
approved state produced each deliverable.

Deploy is a human step from Cloud Shell:

```bash
gcloud builds submit --config cloudbuild.yaml .
```

On the first deployed run after slice 2, each existing coverage plan adopts a
migrated approval signature and **persists it** — a one-time write per plan.
Expected, idempotent, worth watching once rather than being surprised by.
