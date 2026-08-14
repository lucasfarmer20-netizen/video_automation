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

- Branch `main`, commit `5844356`, pushed. The three commits after
  `d515320` are documentation only (audit reports, this handoff, and the
  contract/charter); no implementation changed. Worktree clean apart from unrelated
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

The slice 2 and slice 4 reports were only committed at `8f9dd37`; an audit
pinned to an earlier commit could not read them.

## Closed — do not re-litigate without new evidence

- A-04 warning identity: derived from content; a supplied id is kept as
  `source_id` and is never the disposition key.
- F-01 `save_current_project` writing to the process pointer; F-02 global job
  namespace; F-03 unscoped frontend reads; PF-01 reference registries.
- S2-01 delimiter-ambiguous signature preimage; S2-02 unpersisted transitions;
  S2-03 concurrent plan writes (incl. the Windows transient replace denial,
  closed at `8fda625` and independently verified over 19,200 saves).
- S2-04 concurrent *reads* denied by a concurrent replace — the reader-side half
  of S2-03, closed by `atomic.read_json`. Same class as the out-of-scope
  `os.replace` entry below and fixed anyway, by explicit human override: the
  test asserting it was failing ~9 runs in 10, and a permanently red suite
  trains people to ignore red. Severity Low (workstation-only); the reason was
  signal integrity, not reachability.

## Explicitly out of scope

- Windows-only transient `os.replace` behaviour — settled. The reader-side form
  (S2-04) was fixed once, by human override, for the false-signal reason above;
  that is not a re-opening of this entry and does not license another round on it.
- Cross-process locking and logical lost-update prevention — both documented in
  `backend/atomic.py` as *not provided*, by agreement.
- Slices 0–3 unless a later change touched them.

Prefer defects reachable in production on Linux/Cloud Run over workstation-only
conditions. An earlier round was spent on a Windows-only concurrency case that
cannot occur in the deployed environment.

## Round in flight: slice 5b — the timeline UI bound to slots

Build round, not a remediation round. Frontend only; `backend/` is untouched and
`git diff main -- backend/` is empty. Slice 5's slot model, API and tests were
already built and audited — 5b binds `MultitrackTimeline.tsx` to
`GET /api/timeline/slots` instead of to `project.shots`.

What is new to audit:

- `frontend/src/lib/slots.ts` — the client's reader for the slot payload.
  `isFilled()` returns `slot.placeholder === false` and fails closed; it must
  never re-derive the flag from `media`, and no client state mirrors it (§11.4).
- V1 renders one clip per slot, keyed on `slot.id`, and selection is held as a
  slot id rather than a position — that is what survives a re-plan (§7.1).
- `coverage.summary` is rendered verbatim; the component does not count (§6.2).
- Placeholders carry shot id, slot identity, intended duration, expected media
  and source beat, in words as well as attributes (C5).
- Trims are written through `POST /api/timeline/slot/{id}/trim`; take selection
  through the existing `POST /api/director/shot/{shot_id}` (coverage slots) and
  `POST /api/shot/{beat_id}` (whole-beat slots). No new endpoint, no new field.

Evidence: `frontend/src/lib/slots.test.ts`,
`frontend/src/components/MultitrackTimeline.slots.test.tsx` (20 tests,
`npm test` in `frontend/`), and `scratch/mutate_5b.py`, which applies six
mutations — including one that reproduces the original defect (V1 built from
beats and their file paths) — and reports each killed and the suite restored.

Note for this round: take selection records which take a shot uses; the media in
a slot changes when the server next reports it. The UI states what the server
reports and claims nothing beyond it. That is deliberate, not an oversight — an
endpoint that swaps a rendered sub-clip does not exist and was not added, since
the slot contract is settled.

### Round 1 outcome — split, remediated

Deep pass approved. The built-in reviewer requested changes with four defects,
all real, all reproduced at source before being fixed. None required a backend
change.

1. **V1 drew slot media into an `<img>`** — `DirectorShot.clip` is
   `render/<beat>/<shot>.mp4` and the whole-beat fallback is
   `render/<beat>.mp4`, so every filled slot fired `onError` and hid its own
   frame; V1 had no imagery at all. Now a `<video preload="metadata" muted>`
   with a poster taken from the still the clip was rendered from, where the
   studio already holds one. The server-supplied-poster alternative was not
   taken: it is an API change.
2. **Slot state leaked across projects** — slot ids are `beat_id::shot_id`,
   unique within a film and not across films. Slot state is now stamped with the
   project it was read for and `viewForProject()` refuses to serve it to any
   other; this covered `slots` and `coverage` as well as the takes strip.
3. **"Take 2 · in use"** claimed the slot held a take that only becomes its media
   at the next render. Now "chosen", with the timing said out loud (§11.4).
4. **Build Draft 1 reported success** for a run that stops at the storyboard
   gate. The reply is now reported for what it is, and an uncleared gate is
   stated beside the button. Still not disabled — incomplete coverage must never
   block Draft 1 (§6.2), and disabling would misattribute the block to coverage.

All four notes were taken as well: the mutation harness snapshots every file a
mutation touches; `slotAt`/`VIDEO` are gone; `slotDuration`'s fallback is now the
server's arithmetic clause for clause.

### Round 2 outcome — one High, and it was reachability

Both surfaces requested changes. Three findings, all real, all fixed in one
push. Two of the three were caused by the round-1 remediation itself, which is
the pattern these guardrails predict.

1. **HIGH, §11.3 — the studio could not change project at all.**
   `ProjectSidebar` has always called `onSelectProject(p.rel, p.project_id)`;
   the page wrapper took only `rel` and `handleSelectProject`'s second parameter
   was optional, so the id was silently always `undefined`. `projectIdRef` never
   moved, every later request carried the previous project's `X-Project-Id`, and
   the middleware answers about the project the client *names* over the active
   pointer by design (`main.py:165`). **Pre-existing on main** — introduced by
   `a5ee370`, the mobile-sidebar work, not by this slice — but it made all of
   round 1's cross-project isolation unreachable, which is why it was fixed
   here. `project_id` and the callback parameter are now required types, so
   dropping the argument again is a compile error.

   The lesson worth keeping: no mutation of the timeline could have caught this,
   because the defect was in the seam between the sidebar and the page, and every
   test mounted the timeline directly with hand-made props. There is now a
   page-level test that clicks the sidebar entry a user clicks and watches the
   headers that go out.
2. **The poster restated the claim the badge fix removed.** `posterFor()`
   resolved the take chosen *now*, not the still the clip was rendered from, and
   `POST /api/shot/{id}` reassigns `draft_image` synchronously — so choosing take
   2 repainted V1 with take 2's still while `slot.media` was still take 1's clip.
   Dropped entirely for `#t=0.1` on the media itself, which is by construction
   the media in the slot. Introduced by the round-1 fix.
3. **`trimError` outlived its slot.** Now stamped with the slot it concerns and
   cleared on every blur, including the no-op blur that withdraws a rejected
   value. Introduced by the round-1 fix.

### Round 3 outcome — the rollback gap

One High, fixed in `ec47d14`. `handleSelectProject` committed the new film's
identity before asking the server and never put it back when the answer was no.
`/api/project/select` calls `refuse_if_jobs_running` (`main.py:1201`) and 409s
whenever a job is running, so a refused switch left the screen on film A while
every request named film B — and `directorApi` keeps its own copy of the id
(`directorApi.ts:45`) used for `isStaleReply`, so film A's own replies would
have been discarded as stale while film B's were accepted onto its screen.

Rollback is now the default: the previous id is captured, the attempt runs in a
`try`, and a `finally` restores both pointers unless the open was confirmed.

**Watch item, not a defect:** V1's filled clips paint their frame via
`#t=0.1` with `preload="metadata"`. Chrome and Firefox seek and paint it;
Safari is inconsistent, so a filled clip may read blank there. It degrades to a
blank clip, never a wrong one, which is the property the change was for.
`preload="auto"` on filled slots is the remedy if it shows up in practice — a
one-word change, at the cost of every filled slot fetching its clip. Not done
speculatively.

### Round 4 outcome — the two reviews disagreed, and the weaker condition lost

The built-in surface APPROVED `opened = true` sitting immediately after
`/api/project/select` succeeded; the deep pass called the same line a reachable
High. The deep pass was right, on two checkable facts: `fetchActiveProject`
swallowed its own failures (`catch` + `finally { setLoading(false) }`, nothing
propagating), and `bind_project_context` resolves `X-Project-Id` **over** the
active pointer. The second is what settles it — because the header wins, rolling
the CLIENT back to the previous film makes the client self-consistent: it
displays that film and it talks to that film, and the server's now-stale pointer
is consulted only by requests that name no project, which this studio does not
make once identity is known.

The category error is worth keeping: `opened` recorded **server acceptance**
where the rollback needed **"the studio can safely display this"**. The server
accepting a switch is not the studio being ready to show it, and using the first
to answer the second renders one film while addressing another.

`fetchActiveProject` now returns whether it *installed* identity and data.
Returned rather than thrown: it has 34 call sites and only two of them raise the
loading screen, so throwing would have fixed one and exposed 32 to unhandled
rejections.

**Declared test gap, not a covered line.** `if (installed) setLoading(false)`
cannot be pinned in jsdom — `alert` is a synchronous mock, React batches the
whole failure path into one render, and the end state is identical with or
without the guard. The exposure it prevents is a real-browser paint behind a
blocking `alert`. The mutation was written, run, survived, and is recorded in
`scratch/mutate_5b.py` under `KNOWN_UNKILLABLE_IN_JSDOM` rather than deleted or
left to rot in the pass/fail signal.

### Two sibling handlers with the same identity defect — pre-existing, NOT fixed

Flagged by review, verified, deliberately left alone: they are outside 5b and
each wants its own page-level tests rather than being smuggled into a rollback
fix. Both are unchanged from `main`.

- **`handleCreateProject`** (`page.tsx`) calls `fetchActiveProject()` with the
  previous project's id still in `projectIdRef`. The middleware honours the
  header over the pointer, so the studio is answered about the *old* film and
  never lands on the one it just created.
- **`handleDeleteProject`** leaves `projectIdRef` naming a project that has just
  been moved into `_trash`, where `_context_for` cannot resolve it — so every
  later request 404s and `fetchActiveProject` silently updates nothing. The
  delete response already returns `was_active`, which is exactly the signal
  needed to fix it.

### The harness lied once — hardened

A run reported `restored: PASS` while leaving the ORIGINAL DEFECT mutation
applied in `MultitrackTimeline.tsx`. The pushed branch was unaffected (the file
was never staged), but the claim was false, and it is the claim this slice's
evidence rests on. Root cause not established; the response is that a green
suite is no longer treated as proof the tree is clean. `scratch/mutate_5b.py`
now hashes every file any mutation touches before the run, compares after,
restores from content captured at entry, prints `TREE NOT RESTORED` naming the
files, and exits non-zero. Any `restored: PASS` printed from here is backed by
hashes rather than by a suite that might simply not exercise the mutated file.

### Known cosmetic, deliberately not fixed

After a refresh-failure rollback in `handleSelectProject`, the sidebar still
marks the previous film active while the server's pointer says the new one,
because `fetchProjects()` is skipped on that path. Inert for the same reason the
pointer is inert — every request names its project — and a refetch was
considered and rejected: the alert already tells the user to try again, and
repainting the list mid-failure adds a request on the path that is already
failing. It is the one visible thing left disagreeing. Reviewed and agreed on
both sides.

**Left open, deliberately, and NOT closed by this slice:**
`POST /api/timeline/slot/{id}/trim` accepts a `trim_in` beyond
`intended_duration` and returns a zero-length slot. The inspector no longer
authors one and says why it refused, but the endpoint is unchanged and any other
caller can still reach it. Server-side gap, belongs to whoever owns that
endpoint. Do not record it as fixed.

## Next action: re-verify the Slice 4 remediation

Audit `e2bdf2f`..`d515320` — the remediation itself — against `scratch/codex_adversarial_audit_slice_4.md`.
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
