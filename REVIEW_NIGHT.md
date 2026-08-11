# Overnight review — what changed and what to do

Four agent review rounds plus a targeted verification pass — 285 agents, 18 fix
commits. **Nothing is deployed.** Everything below is on `main`, pushed.

## Deploy

From Cloud Shell:

```bash
gcloud builds submit --config cloudbuild.yaml .
```

`gcloud` is not installed on the workstation, which is the only reason this is
waiting on you.

## What to check first, in this order

1. **Open the Director workspace and edit a shot.** Before tonight, nothing you
   did there reached the server — edits were React state only, LOCK validated the
   unedited plan, and the button silently did nothing behind a green badge. This
   is the largest behavioural change and the one most worth a look.
2. **Open the Take Selector.** It could not be opened at all: the component
   returned before its `useState`, so React threw. Confirm picking TAKE 1 shows
   take 1 as selected — the index was off by one and the *paid* image-to-video
   model uploads whichever still that field points at.
3. **Watch the render log for duration lines.** You should see e.g.
   `s001: 20.86s exceeds what Seedance 2.0 can generate; asked for its maximum
   15s (the remainder freeze-frames)`. Before tonight that beat silently
   generated 5s.

## The two things that were costing money

**Paid clips were re-billed on every retry of a failed compile.**
`compile_coverage` recorded `ds.clip` only after post-processing, so a
normalize/fit failure left a paid mp4 on disk that the resume guard could not
see — and the retry the error message asks you to run bought it again, without
bound. Spike F hit this exact shape. Now the clip is recorded the moment it
lands; a retry re-runs only post-processing. There is a test that counts fal
calls across three failing compiles: 3 before, 1 after.

**Gate 1 had no server-side enforcement on `POST /api/shot/{id}/generate_video`.**
Three other Tier-C paths refuse an unapproved storyboard; this fourth one never
got the check, and buys a $0.15 still on its way to fal. A browser `confirm()`
was the only thing holding a gate CLAUDE.md calls non-bypassable.

## The quality win

`capabilities.py` existed to end hand-written model limits and two call sites
never adopted it. Consequences, all live until tonight:

| model | was sent | reality |
|---|---|---|
| kling, wan, luma | **no duration at all** | each rendered its own 5s default |
| seedance (default) | `"3"` on short beats | enum starts at 4 → 422, beat lost its clip |
| everything | capped at 10s | seedance and wan reach 15s |
| veo | 5.0s rounded **down** to `"4s"` | the planner rounds it **up** to 6s |

On MichaelHeney's 20.86s beats that is **5s of motion versus 15s**. Roughly
**120 fewer frozen seconds** across 12 paid beats — this is a quality change, not
just a bug fix.

## Everything else fixed

- Narration streamed into its final path, so an interrupted TTS left a truncated
  mp3 under the name every later run tests with `.exists()` — permanently
  skipped, and librosa raises on it, taking down `sync_durations` and the whole
  preview build. Now written atomically.
- `shutil.move` cannot move a directory on gcsfuse (`copytree` calls `copystat`
  on directories unconditionally). Every project delete/reset retry silently left
  a full duplicate in `_trash`.
- Config path globals: the pointer file and `config.MANIFEST_PATH` were set by
  different functions and diverged; startup never synced from the pointer at all,
  so a cold container wrote character anchors to the ephemeral `/app`. Project
  switch/create/delete are now refused while a job is running.
- FCPXML dropped **every layered SFX** and ignored `offset_narration`, so the cut
  you approve at Gate 2 and the one Resolve receives were different.
- Firestore upserted beats and never deleted, so shrinking an episode left
  zombies carrying the old script's narration and `approved=true`.
- Deleting a video take left the *deleted* take's pixels in the cut, permanently.
- A duration edit never re-priced the shot — a 4→10s drag under-reported cost by
  66% on the screen where you allocate the Gate-1 budget.
- Generation failures now leave durable ledger rows. Previously 78 successes and
  zero failures were recorded, which is why "19 of 25 beats got 1 take instead of
  3" was unanswerable after the container recycled.

## Known-remaining, deliberately not done

- **`/api/assemble/status` is unauthenticated**, like every GET. Tracebacks no
  longer go into it, but job logs still do.
- **Firestore is not provisioned**, so the dual-store bugs are latent. The beat
  reconciliation fix should land *before* you provision one.
- **`build()` emits no mix state** — gains, fades, bus levels are preview-only by
  design. The FCPXML carries position and length, not the mix.
- **Frontend has no test infrastructure.** Every frontend fix tonight is verified
  by `tsc` and `next build` only. The take-index contract is tested on the
  backend side, not in the component.

## How much to trust this

Every fix has a test verified by reverting the fix and watching it fail, and the
money-path guards are additionally mutation-tested — each clause removed in turn
and confirmed to break a test.

The honest number: **roughly half the fixes written tonight contained an error a
later round found.** Three examples, all mine:

- The round-1 gcsfuse fix did literally nothing — `copy_function` governs file
  copies only, and `copytree` `copystat`s directories regardless.
- The round-2 field-validation fix was a regression that killed the Tier-C budget
  control, and shipped green because that endpoint had no tests.
- The round-4 re-bill guard asked only "is there a file at target?" — and
  `motion.render_shot` writes that exact path, so a **free** parallax render was
  accepted as the paid clip. Reproduced: 0 paid calls, free footage shipped as
  paid, $0.00 recorded. That is worse than the re-billing it replaced, because it
  is silent.

Three tests I wrote were vacuous and were caught by mutation-testing rather than
by reading them: one wrote its fixture to a path the guard never reads, and two
used 2.0s as an "unroutable" length when 2.0s is exactly `wan_2_7`'s floor. All
are fixed and now fail when the code they guard is reverted.

The pattern that worked: **point the agents at the newest code, not the oldest.**
Every round found more in the previous round's fixes than in the original
codebase.

Treat the first paid run as a test. Watch the render log, and check the cost on
the Gate-1 screen against what actually lands.

## Known flaky test

`tests/test_director.py::test_gestural_*` fails intermittently with
`x264 [error]: malloc of size 3325760 failed`. It is memory pressure from running
four agent workflows against this box overnight, it reproduces on unmodified
HEAD, and it is not a defect. `-threads 1` does not help; `-preset ultrafast`
does but shifts output by ~2 frames and breaks the duration assertions the helper
exists to support, so neither was kept. It should stop once the box is idle.

## Test suite

117 tests, pyflakes clean, `tsc --noEmit` and `next build` pass. Runtime deps are
now installed on the workstation, so the FastAPI app can be imported and
exercised — `py_compile` does not catch a `NameError`, and one shipped tonight
before that was set up.
