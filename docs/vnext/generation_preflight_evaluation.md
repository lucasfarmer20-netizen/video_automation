# Generation Preflight — evaluation

**Status: evaluation only. Nothing here is built. No production code was changed
to produce it.**

The question is whether to put a critic in front of every prompt that reaches
fal, and if so, where it sits, what it checks, and what it costs. The answer
below is: **yes for stills, on a narrower scope than proposed, and not first.**
The thing to build first is the telemetry, because every number that would
justify or kill the critic is currently unmeasurable — including the 5–8
regenerations that motivated the request.

Everything asserted about the current system was read out of the code at
`648f81b` and is cited by `file:line`. Where the source brief's figures
disagree with the code, the code wins and the correction is stated.

**Revised after fal's invoice arrived.** A first draft of this document priced
draft stills at `COST_PER_IMAGE = 0.15`. That constant was 3.8× over: fal's
billing API bills `fal-ai/nano-banana` — the endpoint `nano2` actually calls — at
**$0.0398** an image, and `648f81b` corrected the table with provenance
(capabilities.py:237-254). Every figure below is at the corrected rate and says
where it came from. The correction did not merely shrink the numbers; it moved a
conclusion, and §9 is where it lands.

---

## 1. Summary

**Corrected cost model.** The brief's conclusion — rerolled stills, not paid
video, are the dominant generation cost — **holds, but only once you are
rerolling, and by a smaller margin than either the brief or this document's first
draft claimed.** Four of the brief's inputs are wrong (5 shots/beat not 6–10;
$0.56/clip not $0.60–1.51; 15 or 27 beats not 18; a regenerate *click* is 3
images, not 1) and one of mine was (stills are $0.0398, not $0.15). The
corrections run in opposite directions and very nearly cancel: the ratio of
reroll waste to video spend is **2.4–3.9×**, against the brief's ~3×. The brief's
number was right for the wrong reasons. §2.

**And at one attempt per still, video is twice the stills.** $2.99 of stills
against $6.16 of clips on a 15-beat episode. Stills only overtake video at ~2
attempts each. The scoping decision — preflight covers stills, not just
`ai_video` — still holds at the observed reroll rate, but it is a 2–4× argument
now, not an order-of-magnitude one, and it should not be sold as one. §2.4.

**A. Where preflight sits.** Preflight **is** vNext Phase 4, arrived at from the
other end. Phase 4 asks "is this package still the one that was approved?";
preflight asks "is this package any good?" Both are one gate at one moment — the
transition from an approved plan to a dispatched request — and this codebase has
already paid for having two doors onto the same money that disagreed
(`director.compile_coverage` walking past Gate 1, `main.py:3999` consolidating
four route-level checks into one). Build preflight as the *content* half of the
Phase 4 stamp, not as a second gate. §5.

**B. One finding taxonomy.** One shape serves all three consumers. The evidence
is not a design argument — it is that the codebase already contains most of it:
`plan.warnings` + `warning_dispositions` (director.py:493-557) is a finding with a
content-derived stable id, a `kind`, a `detail`, a `suggestion` and a durable
human disposition, and `unresolved_warnings` already hard-blocks locking and
compiling. QCFinding's `category`/`disposition` and slice 6's issue record are the
same object with a different vocabulary. Preflight findings differ from QC
findings in exactly two fields — `stage` (before/after generation) and `subject`
(a prompt vs. a rendered file) — and neither warrants a second schema. §6.

**C. Hard, not advisory.** `BLOCK` + `[Generate Anyway]` is not a gate, and this
repo has already removed the equivalent (`force=true` on the compile route,
main.py:3113-3118). But the honest form of "hard" here is not *block on the
critic's opinion*. It is: **the critic's findings become undispositioned
warnings, and the existing `unresolved_warnings` gate refuses to lock.** The
human's escape hatch is `accepted` — a recorded decision with a name against it —
not a query parameter. That mechanism exists, is already non-bypassable, and
needs no new gate. §7.

**Minimum critic set: two for stills, one for video, and one deterministic
check that is not a model call at all.** The seven proposed critics are argued
down to what the evidence supports. §8.

**What a critic call costs — and this is where the invoice changed an answer.**
~$0.031/shot on Opus 5, ~$0.006 on Haiku 4.5, against a **$0.0398** still.
**An Opus-5 preflight costs 78% of the still it guards.** On the coverage path,
where §2.5 shows there is no reroll to prevent, that is 78% of the asset value
buying zero prevented spend — the money case there is not thin, it is **zero**,
and preflight on that path must be carried by irreversibility alone or not built.
On the beat path, where the rerolls actually are, the unit is a 3-image click at
$0.1194 and break-even is 0.26 clicks per beat against 4–7 wasted ones — that
still works on any model. **The conclusion splits by path, and the model choice
stops being free: Haiku 4.5, not Opus 5.** §9.

**Build first: telemetry.** Three ledger writes and one route, ~1 day, no paid
path touched, shippable alone and useful alone. It is also the only way to find
out whether the rerolls have a cause a *text* critic can see — which is the one
risk that could still kill this feature. §10.

---

## 2. Corrected cost model

### 2.1 What the code actually charges

**The rate.** `capabilities.COST_PER_IMAGE = 0.0398`, derived from
`IMAGE_PRICE` and still env-overridable (capabilities.py:237-254).
`main.py` aliases it as `DRAFT_IMAGE_COST` rather than restating the literal.

The brief's $0.15 is what the constant said until `648f81b`, and the way it was
wrong is instructive: it priced **the model the docstring named**
(`fal-ai/gemini-3-pro-image-preview`) rather than **the endpoint the code calls**
(`assets.NANO2_ENDPOINT` = `fal-ai/nano-banana`, and `nano2` is
`DEFAULT_BACKEND`). The invoice bills the latter at **22.0 images for $0.8756
over 2026-08-15..19, i.e. $0.0398 each exactly**. Source:
`GET https://api.fal.ai/v1/models/usage`, admin key, checked 2026-08-18. This is
the first cost figure in the repo anchored to what was *charged* rather than what
was *advertised*, and the direction is the safe one — every quote a human
consented to was higher than the bill.

`assets.py`'s docstring now records the residue honestly: three claims about
which model serves `fal-ai/nano-banana`, two of them prose, and it declines to
assert one because the invoice settles which endpoint *runs*, not which model
answers (assets.py:10-21). Preflight has no stake in that question; it is noted
because it is the same class of defect this document keeps finding — a name in a
comment standing in for a fact about what the code does.

**Scale, before any of the arithmetic below.** The human's entire fal spend
across all five days of the project is **$3.20**: wan 12s $1.20, kling 20s $1.12,
nano-banana 22 images $0.8756. Every figure in this section should be read
against that, and §9 is where it starts to matter.

**The brief's "$0.15 per attempt" was wrong twice over** — wrong rate, and wrong
quantity on the path where the rerolls happen. There are two still paths with
different take counts:

| Path | Takes per call | Cost per call | Where |
|---|---|---|---|
| Beat draft (`/api/regenerate/{scene_id}`, batch render, video auto-draft) | `_takes(sb)` — `render.variations`, default **3**, clamped 1–8 | **$0.1194** | main.py:1855-1865, 4172, 925, 4051 |
| Draft refine (`/api/shot/{id}/edit_image/{idx}`) | hardcoded **3** | **$0.1194** | main.py:3850 |
| Director coverage compile | `still_takes(ds)` — **1**, or 4 if `identity_critical` | **$0.0398** / $0.159 | director.py:1044-1053 |

So a single click of "Regenerate Still" is **$0.1194** — three images, not one.
If the human's 5–8 attempts are 5–8 *clicks* on a beat, that is **$0.60–0.96**
per accepted beat still, and **15–24 images to look at, not 5–8**.

The take-count finding survives the price correction entirely and is the more
useful half of it: the number of images being reviewed is 3× what the human is
counting, and *that* is a workload fact, not a money fact.

### 2.2 What coverage actually looks like

Measured from the four real plans in `director_plans/`:

| Plan | Shots | Paid (`ai_video`) | `identity_critical` | Beat seconds |
|---|---|---|---|---|
| s003 | 7 | 0 | 0 | 26.95 |
| s007 | 4 | 1 | 0 | 22.72 |
| s011 | 5 | 1 | 0 | 27.04 |
| s017 | 4 | 1 | 0 | 20.30 |
| **total** | **20** | **3** | **0** | — |

**5.0 shots per beat, not 6–10. 0.75 paid clips per beat, not 2.** Zero
`identity_critical` shots, so the 4-take branch of `still_takes` has never fired
in production data.

Episode length: the two real manifests are **15 beats** (`storyboard_manifest.json`,
331s) and **27 beats** (`.calluses`, 314s). Not 18.

### 2.3 What a paid clip actually costs

The brief prices video at $0.60–1.51/clip. That is the Seedance 2.0 rate
(`cost_per_second: 0.3024`, capabilities.py:96). But `capabilities.resolve` sorts
candidates by `(estimated_cost, generate_seconds)` and takes `ranked[0]`
(capabilities.py:464-466), so Seedance is never chosen when Kling 2.1 Standard
(`0.056/s`, capabilities.py:137) can serve the length.

Running `director.quote_shot` over the three real paid shots:

```
s007.02  10.0s  ai_video  ->  kling_2_1_standard  10s  $0.56
s011.02  10.0s  ai_video  ->  kling_2_1_standard  10s  $0.56
s017.02  10.0s  ai_video  ->  kling_2_1_standard  10s  $0.56
```

**$0.56, and the whole 20-shot four-beat sample now quotes at $2.48** (it was
$4.68 at the old still price — the clips did not move, the stills did).

**The invoice confirms the video rows rather than correcting them.** The same
billing data that caught the image constant reports kling 20s at $1.12, which is
$0.056/s exactly — the table's figure (capabilities.py:138). `BILLED_UNITS`
records four kling requests at `duration=5` all billed 5 units
(capabilities.py:318-332). So the video side of this model rests on observed
billing, not transcription, and the $0.56 stands.

Three caveats that keep it honest.

* **Quantity, not just rate.** `648f81b` also found that fal can bill more units
  than were requested: wan v2.7 asked for `duration=4` and was billed **6.0**,
  twice, identically (capabilities.py:266, 306-317). `BILLED_UNITS` now applies the
  smallest observed quantity as a *floor* under the quote — a floor can only
  over-quote, the safe direction — and the comment is explicit that this is an
  observation, not a discovered rule: two requests at one duration cannot
  distinguish a 6-unit floor from a 2-unit step from a 1.5× multiplier. The three
  real paid shots route to kling, where the observed quantity equals the request,
  so they are unaffected. A plan that routed to wan would not be.
* **The rule is not discoverable before the call.** `/v1/models/pricing` returns
  a unit price and nothing else; the endpoint's OpenAPI schema documents what may
  be *requested*, not what is *billed*; and fal's own estimator reproduces the
  same wrong figure because it prices the quantity handed to it
  (capabilities.py:275-293). Any preflight that claims to predict a bill is
  claiming something fal's own API does not.
* **Gestural shots.** A gestural shot excludes any model whose legal length
  exceeds it (capabilities.py:445-446), which removes Kling's 5s/10s grid for
  most coverage lengths and pushes the shot up the price ladder or off it
  entirely. Zero shots in the sample are gestural, so this is latent, not
  observed.

### 2.4 Corrected episode arithmetic

15-beat episode (`storyboard_manifest.json`, 331s), 5.0 shots/beat = **75
director shots**, ~11 paid clips:

| | 1 attempt/still | 5 attempts | 8 attempts |
|---|---|---|---|
| Coverage stills (75 × $0.0398 × n) | $2.99 | $14.93 | $23.88 |
| Paid clips (11 × $0.56) | $6.16 | $6.16 | $6.16 |
| **Total** | **$9.14** | **$21.09** | **$30.04** |
| stills ÷ video | **0.5×** | 2.4× | 3.9× |

Scaled to the 10-minute unit CLAUDE.md's budget is denominated in — both real
manifests are ~5.2–5.5 minutes, so a 10-minute episode at the same 22s/beat
density is 27 beats, 135 shots, ~20 clips: **$16.57 / $38.07 / $54.18.**

**Read the last row before anything else.** At one attempt per still, **video is
twice the stills.** Stills do not overtake video until roughly two attempts each.
The brief's framing — *"rerolled stills, not paid video, are the dominant
generation cost"* — is a claim about the reroll regime, not about the pipeline,
and it is true only inside that regime. At the observed 5–8 attempts it holds by
**2.4–3.9×**.

**Both corrections were real and they very nearly cancel.**

| | video | rerolled stills | ratio |
|---|---|---|---|
| Brief | $22–54 | $108–170 | ~3× |
| This document, first draft | $6–11 | $56–162 | 9–15× |
| **Corrected against the invoice** | **$6–11** | **$15–43** | **2.4–3.9×** |

The brief overstated both sides; the first draft of this document corrected the
video side and inherited the wrong still price, which overstated the ratio in the
other direction. **The brief's ~3× was the right number for the wrong reasons.**

The scoping decision it was offered to support survives — a preflight scoped only
to `ai_video` would still be guarding the smaller line item at observed reroll
rates — but the margin is 2–4×, not an order of magnitude, and it must not be
sold as one. At one attempt per still the scoping argument inverts outright.

The 10-minute single-attempt figure ($16.57) lands inside CLAUDE.md's $15–25
band, which is some evidence the model is right. Worth noting that the band was
set against a still price 3.8× too high, so it has more room in it than anyone
knew.

### 2.5 The finding that reframes the whole problem

**There is no route that re-rolls one Director Shot's still.** This is not a gap
in the UI; it is absent end to end.

* `compile_coverage` reuses stills unconditionally when they exist: `if
  ds.draft_variations:` → reuse, else buy (director.py:1394-1444). `skip_existing`
  does not reach it. A re-compile never re-buys a still.
* `/api/director/shot/{shot_id}` strips `draft_variations` from the request body
  before applying it (main.py:2479-2481), so a client cannot clear them.
* `/api/project/reset` scope `stills` clears `shot.draft_variations` on manifest
  **beats** only (main.py:1967-1968); the `director` scope deletes the entire
  `director/` directory — plan, approval and all.
* The studio's "Regenerate Take" button in `ShotInspectorDrawer.tsx:290-296`
  reaches `performShotAction`, which has no branch for it and returns
  `supported: false` with the text *"has no server route yet"*
  (`frontend/src/lib/directorApi.ts:848-853`). It says so honestly rather than
  pretending — but it does not regenerate anything.

Two consequences for this design:

1. **The 5–8 regenerations are on the beat path, not the coverage path.** They
   are $0.1194 each, and they happen *before* the Director plans coverage over
   that beat. Any preflight aimed at the reroll loop the human described must
   cover `assets._compose_prompt(Shot)` — the beat composer — or it will not
   touch the spend it was built for.
2. **On the coverage path, preflight is the only chance.** One still is bought,
   with no picker and no re-roll. A bad prompt there produces a bad shot that
   stays bad until the plan is destroyed. That is the strongest argument in this
   document for preflight existing at all, and it is not an argument about money.

**The invoice makes the second point stronger, not weaker, and the reason is
worth being explicit about.** At $0.15 a still it was possible to read that
paragraph as a money argument wearing a quality argument's clothes — $11.25 of
un-rerollable stills per episode is a number one can care about on its own. At
$0.0398 it is **$2.99**, and the money reading collapses. What is left is the
argument that was always the real one:

> The coverage path buys exactly one image per shot, offers no way to buy
> another, and no way to look at an alternative. A prompt defect there is not
> expensive — it is **permanent**, until someone destroys the plan and the
> approval with it.

That is a claim about irreversibility, and irreversibility does not get cheaper
when the asset does. A $0.0398 shot you cannot replace is worse than a $0.15 shot
you can, and every gate in this repo is built on the same asymmetry — Gate 1
resolves ambiguity to *not approved* because "a beat wrongly shown as unapproved
costs one click [while] a beat wrongly treated as approved costs money on work
nobody sanctioned" (manifest.py:80-84). The direction of error, not its
magnitude, is what justifies the gate.

So: **§2.5 is the reason to build preflight for coverage. §2.4 is the reason to
build it for beats. They are different arguments and only one of them is about
money** — which is exactly why §9's finding, that the money argument does not
survive on the coverage path, does not kill the feature there.

---

## 3. Current state — what was read

### 3.1 `DirectorShot` — every field that reaches a prompt

director.py:89-183. Fields that reach fal, and how:

| Field | Reaches fal via |
|---|---|
| `prompt` | `_synthetic_shot` → `Shot.prompt` → `_compose_prompt` (director.py:1235, assets.py:607) |
| `motion_prompt` | `generate_paid_clip` directly (director.py:1166) |
| `subject` | **Never sent.** Read only by `character_in_shot` for name detection (director.py:1212) |
| `reference_dependencies` | **Never sent as text.** Name-matched to a character in `character_in_shot`; the *resolved likeness image* is uploaded (director.py:1214, 1389) |
| `camera.duration` | `capabilities.resolve` → `duration_argument` → the request (director.py:1176) |
| `gestural` | Router input only; excludes over-long models (capabilities.py:445) |
| `identity_critical` | Take count only (`still_takes`, director.py:1053) |
| `motion_type`, `backend` | Route selection |
| `purpose`, `shot_size`, `angle`, `composition` | **Never reach a prompt.** Editorial metadata; used by `shot_tier`, the critic's row payload, and the planner's own reasoning |

That last row is the important one and it is not obvious from the field names.
**`shot_size`, `angle`, `purpose` and `composition` are recorded, reviewed,
signed into `plan_signature` (director.py:252-256) — and then dropped.** The
image the model is asked for carries none of them unless the planner happened to
write them into the `prompt` prose. A preflight critic that checks "does this
prompt realise the intended framing" is checking a real and currently unenforced
gap.

Style comes from the parent beat, not the shot: `_synthetic_shot` copies
`beat.style_medium` and `beat.references` down (director.py:1240-1245).

### 3.2 The Director critic

`planner.critique` (planner.py:720-813), model `DIRECTOR_MODEL` default
`claude-opus-5` (planner.py:41), `CRITIC_MAX_TOKENS = 12000` (planner.py:46),
schema `CRITIC_SCHEMA` (planner.py:183-211).

**Categories** (11): `missing_establishing`, `repeated_framing`,
`insufficient_cutaway`, `no_reaction_coverage`, `impractical_duration`,
`identity_risk`, `unnecessarily_expensive`, `timing_mismatch`,
`missing_reference`, `continuity`, `other`.

**How warnings are produced.** One scene-level LLM call over all beats, then two
deterministic checks appended in code — duration arithmetic and router
producibility (planner.py:775-810), on the stated principle that arithmetic and
budget are facts, not opinions.

**Disposition.** `warning_id` derives a stable 12-hex id from
`beat_id|shot_id|kind|detail` (director.py:493-499). `normalize_warnings` always
recomputes it and never trusts an incoming id, preserving a supplied one as
`source_id` (director.py:501-527) — so a changed finding cannot inherit the
decision recorded against the one it replaced. `resolve_warning` records
`resolved` or `accepted` with a note and an author (director.py:537-557).

**What `unresolved_warnings` gates** — three places, all hard:

* `POST /api/director/lock/{beat_id}` — 400 (main.py:2968-2976)
* `POST /api/director/lock_scene` — 400 (main.py:2861-2872)
* `POST /api/director/compile/{beat_id}` — 409, described in the code as defence
  in depth for plans locked before the rule existed (main.py:3201-3209)

**The critic never sees a prompt.** The row payload it is given is
`shot_id, purpose, subject, shot_size, angle, seconds, camera_move, motion_type,
face_visibility, estimated_cost` (planner.py:739-746) — and deliberately not
`reason` or the scene's `visual_strategy`, so it cannot be persuaded by the
planner's own rationale. `prompt` and `motion_prompt` are simply not in it.

This is the cleanest justification for preflight being a *new* critic rather
than an extra category on this one: they read different objects. The Director
critic reads a shot list and asks "can this scene be cut". Preflight reads a
prompt and asks "will this string produce the shot". Merging them would put the
prompt in front of a critic explicitly designed not to see rationale.

### 3.3 The image prompt path

Composition, in order (`assets._compose_prompt`, assets.py:607-624):

```
1.  shot.style_medium           # per-culture historical medium; LEADS
2.  ". "
3.  shot.prompt                 # scene description
4.  _character_clause(shot)     # " The recurring figure keeps these fixed
                                #   identifying features regardless of art
                                #   medium: <anchor>; <anchor>."
```

Anchors come from `characters.json` `structural_anchor`, keyed by the names in
`shot.references`, read directly to avoid an import cycle (assets.py:577-605).
Style/medium is deliberately excluded from anchors.

Then, per take (`generate_for_shot`, assets.py:826-921):

```
5.  + PROMPT_STRATEGIES[strategy]   # one of baseline | depth_staged |
                                    # medium_forward | chiaroscuro
6.  soften_prompt()                 # only on a content-policy retry
```

Strategies rotate by beat ordinal so no strategy gets a permanent slot-0
advantage (`strategies_for`, assets.py:804-819). With `PROMPT_VARIANTS` on
(default) and `n > 1`, the n takes are **n different prompts**, one call each,
not n seeds of one prompt (assets.py:861-885).

**Where the avoidance list goes, by backend:**

* `nano2` (default, Gemini 3 Pro Image): no `negative_prompt` field, so the
  avoidance list is folded into the positive prompt inside `_generate_nano2`
  (assets.py:317-410).
* `flux-cfg`: `NEGATIVE_PROMPT` (assets.py:222-231) applied via NAG
  (`nag_scale`), not real CFG.
* generic handlers: no negative at all; `safety_tolerance` and
  `enable_safety_checker` instead (assets.py:952-975).

**Consequence for preflight: there is no single "the prompt".** There is a base,
four strategy variants, an optional softened rewrite, and a backend-dependent
negative that is sometimes inside the positive string. A preflight that critiques
one of these is not critiquing what fal received. **Critique the base
(`_compose_prompt` output); record the final per take.** `ledger.record_generation`
already stores both — `prompt` (base) and `prompt_final` (only when it differs) —
with `softened` alongside (ledger.py:85-131). That decision was made for the
right reason and preflight should honour it.

### 3.4 The video prompt path

`director.generate_paid_clip` (director.py:1117-1194):

```
prompt = ds.motion_prompt  or  f"Cinematic motion, authentic detail, {ds.prompt}"
if f"{dur_int}s" not in prompt and "second" not in prompt:
    prompt += f" (duration: ~{dur_int} seconds)"
arguments = {"prompt": prompt, "generate_audio": False}
arguments["duration"]  = capabilities.duration_argument(key, want)   # per-model spelling
arguments["image_url"] = fal_client.upload_file(<the chosen still>)
```

Three things a preflight would need to know:

* The clip is driven by a **still that already exists** — `synth.draft_image` or
  variation 0 (director.py:1178-1182). Half the input is an image, not text, and
  a text critic cannot see it.
* `motion_prompt` is short. Measured across the four real plans: 7 of 20 shots
  have one at all, **median 33 characters, max 40** (e.g. `"slow drift"`). There
  is very little text to critique.
* The router may have already changed the shot: `generate_paid_clip` re-resolves
  rather than trusting `ds.backend`, appends `constrained_by` entries and mutates
  `ds.backend` (director.py:1155-1163) — *after* the plan was signed and locked.

### 3.5 `capabilities.py` — what is already built

The registry `VIDEO_CAPS` covers 7 backends (capabilities.py:78-208), each with
`allowed_durations`, `duration_values`, `duration_wire_type`, `min/max_seconds`,
`cost_per_second`, `price_basis`, `price_source`, `price_checked`,
`supports_generate_audio`, `supports_reference_image`,
`supports_character_reference`, `verified`. `CONTINUOUS` is the permissive
fallback, priced at the dearest configured rate on the stated principle that the
one direction money must not err is quoting under (capabilities.py:58-77).

`resolve(intent, prefer)` (capabilities.py:411-475) takes the planner's
vocabulary — `duration, gestural, character_motion, face_visibility,
motion_complexity, needs_character_reference` — filters on character-reference
support and legal duration, excludes over-long models for gestural shots, ranks
by `(cost, seconds)`, and returns `backend, generate_seconds, estimated_cost,
constraints, reason, alternatives`. `legal_durations` returns `None` rather than
clamping when a request cannot be served (capabilities.py:294-323), which is the
distinction the whole module exists for.

**How much of §5 is already built: for video, effectively all of it.** The
routing, the legality check, the constraint provenance and the price are done,
and `director.routed_clip` / `paid_clip_price` / `quote_shot` (director.py:1056-1113)
are already the shot-shaped wrapper. A preflight "capability check" for video
would be re-asking a question `resolve` answers, and `planner.critique` already
emits `impractical_duration` when the answer is no (planner.py:798-810).

**For images: none of it.** `IMAGE_BACKENDS` (assets.py:60-117) is a plain
endpoint table with no `max_prompt_chars`, no `max_reference_images`, no
modality, and no price. vNext §Phase 3 lists exactly these as the extension.
Every image-side capability claim a preflight might make would have to be
invented first — which is why §8 keeps image preflight to things that need no
registry.

### 3.6 `GenerationAttempt` — what is recorded, and what §7 is missing

generation.py:56-90. Present: `id, shot_id, beat_id, attempt, parent_attempt,
status, kind, backend, paid, cost, estimated_cost, outcome_unknown,
idempotency_key, signature, output, error, started_at, finished_at`.

Against vNext's §5 attempt list (code_evaluation.md:207-215), missing:
`initiated_by`, `origin`, `provider_job_id`, `resolved_model`, `parameters`,
`qc_findings`.

Preflight needs, precisely, **three** of those and one that is on neither list:

| Field | Why preflight needs it |
|---|---|
| `initiated_by` (`user\|director\|system\|recovery`) | Distinguishes a deliberate re-roll from a resumed compile. Without it "attempts per accepted asset" counts retries of a crashed job as taste. |
| `parameters` | The prompt actually sent, the strategy, the softenings. Currently only in the *other* ledger (`ledger.py`), joined on image path, and not written at all by two routes (§11). |
| `resolved_model` | `ds.backend` is mutated at generate time (director.py:1163); the attempt records the pre-route value. |
| `preflight` (new) | The findings, verdict and stamp this ran under — so a prediction can be scored against the outcome. Nothing in either ledger can hold it today. |

`begin()` (generation.py:302-370) is the enforcement point and it is already the
right one: it returns `created | duplicate | reused | in_flight`, and the caller
must not spend unless it got `created`. **This is where a preflight stamp check
belongs**, because it is the one function every paid path passes through — the
same argument that produced `require_paid_gate` (main.py:3999-4014).

Two paths currently bypass it. See §11.

### 3.7 Regeneration paths, and whether any records why

| # | Route / control | Buys | Attempt opened? | Prompt ledger row? | Reason recorded? |
|---|---|---|---|---|---|
| 1 | `POST /api/regenerate/{scene_id}` (main.py:4153) | 3 stills, $0.1194 | yes, `record_paid_drafts` | yes | **no** |
| 2 | `POST /api/shot/{id}/edit_image/{idx}` (main.py:3816) | 3 stills, $0.1194 | **no** | **no** | **no** |
| 3 | `POST /api/shot/{id}/generate_video` (main.py:4026) | ≤3 stills + 1 clip | stills yes; clip separately | stills yes | **no** |
| 4 | `POST /api/render` batch (main.py:900-930) | 3 stills/beat | yes | yes | n/a (first pass) |
| 5 | `POST /api/director/compile/{beat_id}` (main.py:3109) | 1 still/shot + clips | yes | yes | n/a (never re-buys) |
| 6 | `POST /api/shot/{id}/delete_image/{idx}` (main.py:4253) | nothing — **destroys** a take | n/a | **no** | **no** |
| 7 | `POST /api/shot/{id}` `chosen_variation` (main.py:3644) | nothing — selects | n/a | `record_choice` | no (field exists, unused) |
| 8 | `ShotInspectorDrawer` "Regenerate Take" | nothing — no route | n/a | n/a | n/a |

**Not one of them records why.** The channel exists and is unused:
`ledger.record_choice` accepts `reason` and `scores` and neither is ever passed
from a studio route (main.py:3645 sends only `scene_id`, `path`, `source`); the
only caller that fills them is the identity spike (spike_identity.py:298).
`ledger.record_rejection` — which takes a `reason` and documents that *"a take
that is merely not chosen is far weaker signal"* (ledger.py:211-226) — **has no
callers at all.** It was written for this and never wired up.

The rejection vocabulary is already chosen, in the `record_choice` docstring
(ledger.py:196-197): `face, identity, motion, composition, lighting, historical,
performance, camera, continuity, other`.

### 3.8 The paid gates

| Gate | Where | Asks |
|---|---|---|
| Gate 1, route form | `require_paid_gate` (main.py:3999-4023) | Is `storyboard_approved` **`is True`**? |
| Gate 1, coverage form | `compile_coverage` (director.py:1298-1311) | Same question, same function, for the second door onto Tier C |
| `approval_is_explicit` | manifest.py:64-90 | `value is True` and nothing else. Not truthiness — `"no"` is truthy and once cleared Gate 1 |
| Plan approval currency | `load_plan` → `invalidate_approval` (director.py:455-481) | Does the approval still cover the plan? Drift drops it to `draft` on read |
| Warning gate | `unresolved_warnings` (director.py:530-535) | Has a human decided about every critic finding? |
| Beat staleness | `beat_staleness` (director.py:397-429) | Was the narration rewritten under the plan? |
| Quote binding | `signature_is_explicit` (director.py:352) + compile route (main.py:3236-3251) | Did the caller say which plan they agreed to spend on? Required, not optional |
| Ledger gate | `generation.begin` (generation.py:302) | Is this request new, duplicate, reusable, or already in flight? |

The compile route runs them in a deliberate order (main.py:3146-3251):
signature mismatch → draft/drift → beat staleness → unresolved warnings →
signature present → dispatch. The comment at main.py:3225-3234 explains the
ordering rule: each check should tell the caller something true about *their*
plan, and "lock it first" is better advice than "sign your request".

**Preflight has exactly one place to sit in that chain, and it is not in the
chain at all** — see §5.

---

## 4. What the current system already does that preflight would duplicate

Worth stating before proposing anything, because three of the seven proposed
critics are already implemented:

* **Duration / capability legality** — `capabilities.resolve` + the
  `impractical_duration` warning (planner.py:798-810). Deterministic, free,
  already blocking.
* **Cost sanity** — `quote_shot` (director.py:1097), `unnecessarily_expensive`
  in the critic vocabulary, and the signature-bound quote on the compile route.
* **Missing reference / identity risk** — `character_in_shot` (director.py:1198)
  detects a named character without a likeness and logs at compile
  (director.py:1392-1396); `identity_risk` and `missing_reference` are already
  critic categories; `shot_tier` escalates `face_visibility >= moderate` to
  TIER_CREATIVE (director.py:583-585).

A preflight that re-raises these produces duplicate findings on the same shot
under two different ids — which is precisely the "two registries that quietly
disagreed" failure the codebase has already shipped twice (director.py:568-570).

---

## 5. Question A — where preflight sits relative to the existing gates

### Preflight IS Phase 4. It is not a second gate.

Phase 4 as specified (code_evaluation.md:269-273):

> stamp covering spec, prompt, resolved refs and capability profile; invalidate
> on any change; hard-block generation on an unstamped or stale package. Extends
> the existing approval signature rather than replacing it.

Read carefully, that is a *freshness* stamp: it proves the package has not
changed since it was approved. It says nothing about whether the package is any
good. Preflight is the missing predicate — the thing that makes a stamp worth
having. A stamp that certifies "this is the same bad prompt you approved" is
mechanically correct and useless.

They are the same object because they are computed at the same moment over the
same inputs and consumed by the same check. Concretely, the stamp becomes:

```
preflight_stamp = {
    "signature":  plan_signature(plan),        # existing, director.py:338
    "prompt_hash": sha256(_compose_prompt(...)),
    "capability":  resolve(intent),            # existing, capabilities.py:411
    "findings":    [Finding, ...],             # new — §6
    "verdict":     "clean" | "flagged",
    "at": iso8601, "by": model_id,
}
```

Everything except `findings` and `verdict` is Phase 4 as written. Everything
except the first three lines is preflight. Splitting them produces two artefacts
that must be invalidated together and will eventually disagree about when.

### Why the codebase should not carry two gates here

This is not an aesthetic preference; it is the repo's own most expensive lesson,
recorded twice in comments:

* `require_paid_gate` exists because Gate 1 was attached to *routes* rather than
  to the *spend*, and one of four routes never got it (main.py:4004-4008).
* `compile_coverage` acquired its own Gate 1 check because coverage was a second
  door onto the paid tier that walked straight past the first
  (director.py:1298-1311) — and the comment is explicit that asking the same
  question *the same way* is the point.

A preflight gate placed beside the approval gate would be a third door onto the
same money with a third notion of "cleared". A preflight gate placed *inside* the
approval gate cannot diverge from it.

### The precise seam

**Compute** preflight where the prompt is composed and the plan is signed —
i.e. at lock time, over the plan, alongside `plan_signature`.

**Enforce** it in `generation.begin` (generation.py:302), which is the single
function every paid path traverses and which already refuses to return
`"created"` for four distinct reasons. Adding a fifth — *the package carries no
current preflight stamp* — extends a refusal that callers already handle rather
than inventing one.

Two consequences that make this the cheap option:

* The two routes that currently reach fal **without** calling `begin` (§11.1,
  §11.2) would have to be brought onto it first. That is a defect fix that is
  independently worth doing and is listed as slice 0.
* `begin` already fails closed and its contract is stated in one place. Nothing
  new has to be taught to any call site.

---

## 6. Question B — one finding taxonomy, or several?

**One. It already mostly exists, and it is `plan.warnings`.**

### The three consumers

| Consumer | Status | Shape today |
|---|---|---|
| Director critic | **built** | `{beat_id, shot_id, kind, detail, suggestion}` + derived `id` + `warning_dispositions[id] = {decision, note, by}` (planner.py:183-211, director.py:493-557) |
| vNext Phase 5 `QCFinding` | specified | `{id, attempt_id, category, severity, confidence, timestamp_s\|frame, detail, disposition}` (code_evaluation.md:238-244) |
| V1 slice 6 Refine issue | **deferred to avoid exactly this** | "issues attach to an exact shot, timeline position, audio event, scene or grade region; severity, blocking vs non-blocking, a diagnosis, the smallest suggested fix, and the stage responsible" (filmcraft_v1_roadmap.md:105, 118-121) |
| Preflight | proposed | a prediction about a prompt |

The roadmap already says of QCFinding: *"This is the same object as V1 slice 6's
Refine issue. Build once."* (code_evaluation.md:245). Preflight is the fourth
producer of the same record, not a fifth kind of thing.

### The proposed shape

```
Finding
  id           derived from content, never supplied      # director.warning_id
  stage        director | preflight | qc | refine        # NEW — who produced it
  subject      {beat_id, shot_id, attempt_id?,           # union, all optional
                timestamp_s?, frame?, region?}
  kind         one vocabulary, partitioned by stage      # see below
  severity     blocking | advisory
  detail       one concrete sentence, names the shot
  suggestion   the smallest change that fixes it
  confidence   0..1, optional; absent means "not a model judgement"
  disposition  {decision: resolved|accepted, note, by, at}
```

Everything except `stage`, `severity` and `confidence` is `plan.warnings` today.
`subject` is the union the roadmap's slice-6 exit criterion already demands.

### Why one vocabulary with a `stage` partition, not one flat enum

QCFinding's categories (`identity|motion|physics|artifact|prompt_adherence`) and
the Director's (`missing_establishing|repeated_framing|…`) are not competing
lists — they are disjoint, because they describe different failure surfaces.
`prompt_adherence` cannot be raised before generation; `missing_establishing`
cannot be raised from one rendered file. Forcing them into one flat enum
produces an enum where two-thirds of the values are illegal in any given
context, which is how a schema stops constraining anything.

`stage` makes the partition explicit and checkable, and it is also the field
slice 6 needs anyway to encode *"the stage responsible"* for routing
(filmcraft_v1_roadmap.md:121-123).

### Where it breaks, honestly

Two places, and both are survivable:

1. **`severity` is new and `plan.warnings` has no equivalent.** Today *every*
   Director warning blocks locking. Introducing `advisory` weakens a gate that
   currently has no exceptions. The mitigation is not to introduce it for the
   Director at all: migrate existing warnings as `blocking`, and let only
   preflight emit `advisory`. If that turns out to be a distinction without a
   difference, drop the field — do not widen the Director's gate to justify it.

2. **The disposition key is content-derived, and a preflight finding's content
   changes when the prompt does.** That is correct behaviour — an accepted
   finding about an edited prompt *should* need re-deciding — but it means a
   human who accepts "this prompt describes two moments" and then rewords the
   prompt without fixing it gets asked again. `normalize_warnings` already
   documents and intends this (director.py:507-515). It is a feature; it will
   read as friction; say so in the UI rather than defeating it.

**Recommendation: extend `plan.warnings` to the shape above with a migrate-on-read
in `normalize_warnings`, and build Phase 5 / slice 6 against it.** That is one
schema, one id function, one disposition store, and one gate — and three of the
four are already written and tested.

---

## 7. Question C — advisory or hard?

**Hard. And the mechanism to make it hard already exists, so "hard" costs
nothing to build.**

`BLOCK` plus `[Generate Anyway]` is not a verdict and a button; it is two
contradictory claims about the same decision. This repo has already removed the
equivalent, and the comment is worth quoting because it settles the question
without further argument (main.py:3113-3118):

> There is no force flag. There used to be, and `force=true` skipped the draft
> check entirely — so any caller could send an unapproved plan into a compile
> that generates stills and, for `ai_video` shots, buys paid video. Approval is
> the whole boundary between a proposal and spending money on it, and a query
> parameter that steps over it is not a gate.

### But "hard" must not mean "the critic's opinion blocks the human"

That would be the wrong gate, and it would be wrong for a specific reason: a
preflight finding is a *prediction*, and no prediction should be able to
permanently refuse work a human wants done. Blocking on an unfalsifiable model
opinion is how a gate becomes something people route around — and this codebase
has no route-around left to offer them, which makes it worse, not better.

The right formulation is the one already in production:

> **Preflight findings are undispositioned warnings. `unresolved_warnings`
> refuses the lock. The human clears them by deciding — `resolved` (they changed
> the prompt) or `accepted` (they understood and kept it) — with a note and a
> name attached.**

That is hard by every test that matters:

* Nothing bypasses it: no query parameter, no truthy string, no bulk action.
  `resolve_warning` validates the decision value and raises on anything else
  (director.py:546-548).
* It cannot be cleared by accident: locking a scene is explicitly not an answer
  to a specific finding (director.py:203-208).
* It is **not** a veto: `accepted` is always available, takes one click, and
  leaves a durable record of who accepted what and why.
* It needs no new code path — the gate is at main.py:2968, 2861 and 3201 today.

**Name it honestly: this is a hard gate on *review*, not a hard gate on
*content*.** The human cannot generate without having looked at every finding.
They can generate against every one of them. That distinction is the whole design
and it should be stated in the UI in those words, not softened into "warnings".

### One thing that must not happen

`[Generate Anyway]` must not be implemented as an auto-`accepted` disposition on
all outstanding findings. That is `force=true` wearing the audit log's clothes: a
single click that clears N decisions produces a record indistinguishable from N
considered decisions, and the record is the only thing making the gate real. If a
bulk control is wanted, it must write one disposition per finding *and* mark them
`bulk`, so the ledger can tell the difference later.

---

## 8. The minimum critic set

The source brief lists seven. The evidence supports **two model calls for
stills, one for video, and one deterministic check that is not a model call at
all.** Each is justified by something specific in the code or the measured data;
the four that are cut are cut for stated reasons.

### Keep

**P1 — One photographable instant. (stills, model, load-bearing.)**
*"Does this prompt describe a single moment that a camera could capture, or does
it describe a sequence, a duration, a before-and-after, or a claim about
causation?"*

This is the one the brief names load-bearing and the code agrees. `_compose_prompt`
prepends 322 characters of `style_medium` and appends the character clause, so a
prompt that already contains two moments is a prompt where the medium and the
identity anchor are competing for attention with a contradiction — and the
measured beat prompts are long enough for this to happen (median 309 chars,
observed max 542; one real beat prompt names a diagram, a lineage chart, lettered
text, gold ink and silhouettes in one image).

**P2 — Intent realisation. (stills, model.)**
*"The shot is specified as `{shot_size} {angle} {purpose}`. Does the prompt text
express that framing? Name what is missing."*

Justified directly by §3.1: those four fields are planned, reviewed and signed,
and then **never reach fal**. This is not a hypothetical prompt-quality concern;
it is a documented data loss between the plan a human approved and the request
that gets made. It is also the finding most likely to explain a reroll that the
human would describe as "it's not the shot I asked for".

**P3 — Motion complexity vs. clip length. (video, model.)**
*"Given `motion_prompt` and `generate_seconds`, is the described movement
completable in that time on a start-image model?"*

The brief calls motion complexity load-bearing for clips and that is right, but
note how little there is to work with: 7 of 20 real shots carry a `motion_prompt`
at all, median 33 characters. This critic will most often find *absence* — a paid
clip with no motion direction, driven only by `f"Cinematic motion, authentic
detail, {ds.prompt}"` (director.py:1166) — which is itself the finding worth
raising. At ~11 paid clips per episode this runs 11 times and costs cents.

**P0 — Composition-time completeness. (both, deterministic, not a model call.)**
Free, instant, and it catches the failures that are certain rather than
predicted:

* `_compose_prompt` returned an empty or medium-only string (one of the 20 real
  shots has `prompt == ""`).
* A named character in `subject`/`prompt` resolves via `character_in_shot` to a
  known character with **no** `reference_image`, and `face_visibility` is
  `moderate` or `high`. Today this is a `log()` line during compile
  (director.py:1392-1396) — after the money is committed — and the code itself
  says "expect a different face". Moving it before the spend is a strict
  improvement at zero cost.
* `soften_prompt` would fire, i.e. the prompt contains a known
  content-policy trigger (assets.py:638-661). The human should see the rewrite
  before it is silently applied, not discover it in a ledger row.

P0 should ship even if every model critic is rejected.

### Cut, with reasons

| Proposed critic | Why cut |
|---|---|
| Capability / duration legality | Already built and already blocking: `capabilities.resolve` + `impractical_duration` (planner.py:798-810). Re-raising it creates two ids for one problem. |
| Cost sanity | Already built: `quote_shot`, the signature-bound quote, `unnecessarily_expensive`. |
| Reference resolution | Absorbed into P0 as a deterministic check. It is a lookup, not a judgement — asking a model to do it is paying for a `dict.get`. |
| Style/medium consistency | **Not currently possible to get wrong.** `_synthetic_shot` copies `beat.style_medium` down unconditionally (director.py:1240), so coverage cannot drift in medium from its beat. A critic for an impossible failure trains people to ignore critics. |

### What each costs to run

**P1 and P2 should be one call, not two.** They read the same inputs and return
findings into the same list; two calls doubles the input tokens to halve nothing.
They are listed separately above because they are separately justifiable and
separately removable, but priced below as one call.

15-beat episode: 75 coverage shots + 15 beats = 90 still-critic calls, 11 motion
calls.

| Critic | Calls | Haiku 4.5 | Opus 5 |
|---|---|---|---|
| P0 completeness | 0 (code) | $0.00 | $0.00 |
| P1+P2 still critic | 90 | $0.54 | $2.79 |
| P3 motion critic | 11 | $0.07 | $0.34 |
| **total** | | **$0.61** | **$3.13** |

**The Opus 5 column is roughly the human's entire fal spend to date ($3.20), to
guard an episode whose stills cost $2.99.** Read §9 before choosing a model; on
the corrected numbers this is no longer a rounding error and the choice is no
longer free.

Against beat-path reroll waste of **$7.16–12.54** on the same episode (15 beats ×
4–7 wasted clicks × $0.1194), the Haiku column is comfortable everywhere and the
Opus column is comfortable only on the beat path.

---

## 9. What a critic call costs

Model prices, first-party Anthropic API, per million tokens: **Opus 5 $5 in /
$25 out; Sonnet 5 $3/$15 ($2/$10 promotional through 2026-08-31); Haiku 4.5
$1/$5.**

**Method, stated so it can be checked:** token counts are estimated at ~4
characters per token from measured string lengths in this repo, not obtained from
`count_tokens` (that would mean sending this project's content to an external
service during a read-only evaluation). Treat them as ±30%.

**That error bar mattered less at $0.15 than it does at $0.0398.** The first
draft of this document said the conclusion had "about 20× of headroom, so the
imprecision does not reach it". At the corrected still price the headroom on the
Opus 5 row is **1.3×**, and ±30% *does* reach it. The Haiku row keeps ~6× and is
still safe. Anyone proposing to run this on an Opus-tier model should measure the
tokens properly before committing to it; anyone proposing Haiku need not.

Per-shot still critic (P1+P2 combined):

```
input   system prompt                     ~700 tok
        composed prompt (~850 chars)      ~220 tok
        shot metadata + schema            ~200 tok
                                        ---------
                                         ~1,120 tok

output  findings JSON                     ~250 tok
        thinking (adaptive, billed as output)  ~750 tok
                                        ---------
                                         ~1,000 tok
```

| Model | Input | Output | **Per call** |
|---|---|---|---|
| Opus 5 | $0.0056 | $0.0250 | **$0.031** |
| Sonnet 5 (promo) | $0.0022 | $0.0100 | **$0.012** |
| Haiku 4.5 | $0.0011 | $0.0050 | **$0.006** |

### The break-even, and it splits by path

**Say it plainly, because it is the most useful sentence in this document: on the
coverage path an Opus-5 preflight costs 78% of the still it guards, and prevents
nothing, because there is nothing to prevent.**

The unit differs by path, and that is the whole finding:

**Coverage path** — one still, $0.0398, no picker, no re-roll (§2.5):

| Model | Per shot | Break-even | As a share of the asset |
|---|---|---|---|
| Opus 5 | $0.031 | > 0.78 rerolls/shot | **78%** |
| Sonnet 5 (promo) | $0.012 | > 0.30 | 30% |
| Haiku 4.5 | $0.006 | > 0.15 | **15%** |

The break-even column is unreachable here **at any price**, because the observed
reroll rate on this path is exactly **zero** — `compile_coverage` never re-buys a
still and no route exists to make it. There is no arithmetic under which
coverage-path preflight pays for itself in prevented spend. Its entire
justification is §2.5's irreversibility argument, and it should be built and
defended on that basis or not built.

That does not make it indefensible — 15 cents on the dollar to stop a permanent
defect in the only take that will ever exist is an easy trade, and one this repo
makes routinely. **78 cents on the dollar is not.** So: Haiku 4.5, and the model
choice is now load-bearing rather than a footnote.

**Beat path** — one click, three images, $0.1194, and this is where the observed
4–7 wasted attempts actually are:

| Model | Per beat | Break-even | As a share of one click |
|---|---|---|---|
| Opus 5 | $0.031 | > 0.26 clicks/beat | 26% |
| Sonnet 5 (promo) | $0.012 | > 0.10 | 10% |
| Haiku 4.5 | $0.006 | > 0.05 | 5% |

Against 4–7 wasted clicks per beat this clears comfortably on every model,
including Opus 5. **Preflight pays for itself on the beat path and does not on
the coverage path, and the two need to be argued separately rather than as one
feature.**

Latency: one non-streaming Haiku call at this size is ~2–4s; Opus 5 with adaptive
thinking is ~8–20s. Across 90 shots that is a job, not a request — it belongs
behind `start_job` at lock time, where the human is already waiting for the
planner, not in the compile request path. At Haiku that is ~3–6 minutes for an
episode; at Opus 5 it is ~15–30, which is its own argument.

**The caveat that could still kill it, and it is not a cost caveat.** All of the
above assumes preflight can *see* the cause. A critic that reads only text cannot
predict face drift, model randomness, an anatomy failure, or the case Spike A
measured — anchor text alone producing four different men across four takes
(spike_identity.py:61-63). If the human's rerolls are dominated by those, a
prompt critic prevents approximately nothing and $0.61/episode buys noise.
**Nothing in the system currently records which it is.** That is §10, and it is
why §10 is first — and the invoice has just made it more urgent, not less: the
smaller the prize, the less margin there is for building against a guess.

---

## 10. Telemetry — the smallest change, and yes it ships alone

Three numbers are wanted: **first-pass acceptance, attempts per accepted asset,
regeneration reasons.** Here is exactly how far the existing code already gets,
because the answer is "most of the way".

### Already recorded

* `ledger.record_generation` — one row per image with `scene_id, path, strategy,
  prompt, prompt_final, softened, backend, batch, slot, style_medium,
  motion_type` and an open `extra` dict (ledger.py:85-131).
* `ledger.record_failure` — one row per call that raised (ledger.py:134-171).
* `ledger.record_choice` — one row per human selection, with unused `reason` and
  `scores` parameters (ledger.py:184-208).
* `ledger.record_plan` / `record_plan_outcome` — planner proposals and whether
  they survived to `locked` (ledger.py:241-291).
* `generation.GenerationAttempt` — every paid attempt with cost and status.

**Attempts per accepted asset is already derivable** from `record_generation`
rows grouped by `scene_id` and counted against the last `choose` row — that is
essentially what `summary()` does for strategy win rates (ledger.py:366-421). It
just has no reader that asks the question in those terms.

### The gap, and the smallest change that closes it

| # | Change | Size | Why |
|---|---|---|---|
| T1 | Call `ledger.record_rejection` from `delete_image_variation` (main.py:4253) with a `reason` from the request body | ~4 lines + a UI select | Deleting a take is the strongest rejection signal in the product and it currently writes nothing. The function is already written and has **zero callers** (ledger.py:211). |
| T2 | Pass `reason` through `/api/shot/{scene_id}` when `chosen_variation` is set (main.py:3644-3646) | ~2 lines + a UI select | The parameter already exists and is already documented with its vocabulary. |
| T3 | Add `regen_of` + `reason` to `/api/regenerate/{scene_id}` (main.py:4153) and pass them into `record_generation(extra=…)` | ~5 lines | Turns "N images exist for this beat" into "attempt 3, because the face was wrong". `extra` is designed for exactly this — a self-describing JSONL row, no migration (ledger.py:127-131). |
| T4 | `GET /api/prompts/telemetry` — first-pass acceptance, attempts per accepted asset, reason histogram, all per project and overall | ~60 lines, reads only | The numbers are useless if reading them means SSHing to a bucket. `/api/prompts/ledger` (main.py:3397) is the pattern to copy. |
| T5 | Open a `generation.begin` attempt in `edit_image` (main.py:3816) and write ledger rows for its output | ~15 lines | Not telemetry — a money defect. See §11.1. Grouped here because the same edit closes both. |

**Total: ~90 lines of backend, two `<select>` elements, one new read-only route.
One day.**

### It ships alone, and it should

* It touches no prompt, no gate, no signature, no manifest field. T1–T4 are
  additive JSONL writes into a ledger whose module docstring already promises
  *"Nothing in here may raise into a render"* (ledger.py:30-32).
* It is the acceptance criterion for everything else. The claim "preflight
  reduced attempts per accepted still from 6.2 to 2.1" is checkable after this
  and unfalsifiable before it.
* It is the only way to learn whether the rerolls have a cause a text critic can
  see. If the reason histogram comes back 70% `face`/`identity`, the right build
  is not preflight — it is character reference coverage, and Spike A already
  measured that fix. **Building preflight first risks spending the slice on the
  wrong defect.**

One deliberate omission: **no second ledger.** T1–T3 write into
`prompt_ledger.jsonl`, which is already cross-project, append-only, and
race-free by construction (ledger.py:68-84). T5 writes an attempt into the
existing generation lineage. Nothing new is created to hold generation truth.

---

## 11. Defects found. Written down, not fixed.

Per the brief. None of these was touched.

**11.1 — `edit_image` spends money with no record on either side.**
`POST /api/shot/{scene_id}/edit_image/{var_idx}` (main.py:3816-3878) calls
`fal_client.upload_file` and `assets.generate_image_edit(n=3)` — three paid
images, ~$0.1194 — and:

* opens **no** `generation.begin` attempt, so `spend()` and `at_risk()` cannot
  see it;
* writes **no** `ledger.record_generation` row, so the prompt that produced the
  images is discarded;
* downloads and appends the results by hand rather than through
  `generate_for_shot`, so it bypasses the strategy rotation, the softener path
  and the ledger together.

This is the same defect class as the one `record_paid_drafts` was written to
close — *"money spent, no record"*, contract §11.4 (main.py:478-512) — and the
same class as the one fixed inside `compile_coverage`, where stills bought during
a compile were invisible to `spend()` (director.py:1396-1412). It is a third
instance of a class the codebase has closed twice. It also has no
`require_paid_gate` call, unlike `generate_shot_video` (main.py:4039) which
acquired one for the same reason.

**11.2 — the `n=3` in `edit_image` is a fourth copy of a take count.**
main.py:3850 hardcodes 3 where every other path calls `_takes(sb)`, which exists
precisely because the literal 3 was at five call sites (main.py:1855-1858).

**11.3 — deleting a take destroys evidence silently.**
`delete_image_variation` (main.py:4253-4283) unlinks the file and writes no
ledger row. The strongest taste signal the human produces is the one the system
does not keep. Note also that the image is `unlink`ed, while `/api/project/reset`
moves media to `_trash/` rather than deleting it (main.py:1883) — two different
answers to the same question about destructive operations.

**11.4 — "Regenerate Take" is a button for a route that does not exist.**
`ShotInspectorDrawer.tsx:290-296` → `performShotAction`
(`frontend/src/lib/directorApi.ts:848-853`). It reports the block honestly rather
than pretending to work, which is right — but it is presented identically to
"Alternate Angle", which does work. A reviewer cannot tell them apart until they
click.

**11.5 — the router mutates a signed plan at generate time.**
`generate_paid_clip` writes `ds.backend = key` and appends to
`ds.constrained_by` (director.py:1163-1167) *after* the plan was locked and the
quote bound to `plan_signature`. `backend` is a material field
(director.py:255); `constrained_by` is not. So a compile can change a field that
is part of the signature the human consented against. The change is a correction
(a stale backend re-resolved) and the quote is taken before it, so this is not a
live over-charge — but it means `plan_signature(plan)` after a compile need not
equal `approved_signature`, and the compile route's drift check
(main.py:3168-3183) would report an approval drift caused by the compile itself.
Worth a test before Phase 4 stamps anything.

---

## 12. The complexity score

**Emit it. Do not act on it. Do not display a threshold.**

The proposed 0–3 / 4–6 / 7–9 / 10+ bands are invented, and the brief says so.
There is a precedent in this repo for what happens next: `capabilities.CONTINUOUS`
carries `verified: False` and a comment that assumption *"is exactly how a 3s
request came back as a 5s clip"* (capabilities.py:54-56); the whole module exists
because a hardcoded `max(3, min(10, ...))` was applied to every model. A
threshold nobody measured is that hardcoded clamp in a new place.

So:

* Compute `complexity` and write it to the preflight stamp and the ledger row.
* Do not branch on it. No routing, no tier escalation, no auto-block.
* Show it to the human as a number with no colour and no verdict, or not at all.
* Revisit when the T4 telemetry can answer: *does complexity correlate with
  attempts-per-accepted?* If it does not, delete the field rather than tuning the
  bands.

The same applies to `confidence` on a finding. Record it; do not filter on it
until something has scored predictions against outcomes.

---

## 13. Bounded slices

Sized to match the recent single-defect, reviewable-in-one-sitting shape. Each
is independently shippable and independently revertible.

**Slice 0 — close the unrecorded-spend hole. (~half a day.)**
`edit_image` opens a `generation.begin` attempt, calls `require_paid_gate`, uses
`_takes(sb)`, and writes ledger rows. Pure defect fix, no new concept, and it is
a precondition for `begin` being the enforcement point in §5.
*Exit:* every fal image call in the codebase passes through `begin` and writes a
`record_generation` row. A test asserts it by enumerating `fal_client` call sites.

**Slice 1 — telemetry. (~1 day.)** T1–T4 from §10.
*Exit:* `GET /api/prompts/telemetry` reports first-pass acceptance, attempts per
accepted asset and a reason histogram; deleting or choosing a take in the studio
writes a reason; regenerating records what it is a regeneration of.
**Ship this and then wait for real data before slice 3.**

**Slice 2 — P0, the deterministic preflight. (~1 day.)**
No model call. Empty/medium-only prompt, unreferenced character at
`face_visibility >= moderate`, would-be-softened prompt. Emitted as
`plan.warnings` entries with `stage: "preflight"`, so the existing
`unresolved_warnings` gate picks them up with no new gate.
*Exit:* a plan whose shot names a character with no likeness cannot lock without
a recorded decision. The compile-time `log()` at director.py:1392 becomes
unreachable for that case.

**Slice 3 — the Finding schema. (~1 day.)**
Extend `plan.warnings` to §6's shape with a migrate-on-read in
`normalize_warnings`. `stage` defaults to `"director"` for existing rows. No new
producers.
*Exit:* the same id function, disposition store and gate serve Director and
preflight findings; a test asserts an existing plan file loads unchanged and its
recorded dispositions still apply.

**Slice 4a — P1+P2 on the BEAT path. (~2 days.)**
One model call per beat, over `assets._compose_prompt(Shot)`, at draft time.
This is the half that pays for itself: §9 puts break-even at 0.05–0.26 clicks per
beat against 4–7 observed. `PREFLIGHT_MODEL` env override in the
`DIRECTOR_MODEL` style; **default Haiku 4.5, and the default is now a decision
rather than a detail** — see §9.
*Exit:* regenerating a beat still runs the critic first; findings are shown and
dispositioned; the run cost is in the job log. **Gated on slice 1 showing the
rerolls have a text-visible cause.**

**Slice 4b — the same critic on the COVERAGE path. (~1 day on top of 4a.)**
Same call at lock time, behind `start_job`, findings as `stage: "preflight"`
warnings so the existing `unresolved_warnings` gate carries them.
**Justified by §2.5's irreversibility argument and explicitly not by §9's
money argument, which does not close on this path at any model price.** Split
from 4a so that judgement is made deliberately and can be declined on its own
without losing the half that pays.
*Exit:* locking a scene runs the critic; findings appear beside Director
warnings and are dispositioned the same way.

**Slice 5 — the Phase 4 stamp. (~2 days.)**
The stamp of §5 written at lock, checked in `generation.begin`. This is the
slice the risk register flags — *"Phases 3 and 4 touch the path that spends
money… every change here needs adversarial review and mutation-sensitive tests
before merge"* (code_evaluation.md:293-296). Do not fold it into slice 4.
*Exit:* `begin` returns a fifth disposition for an unstamped or stale package;
every paid path handles it; a mutation that weakens the stamp check fails a test.

**Slice 6 — P3, the motion critic. (~1 day.)**
11 calls per episode. Lowest value of the three, do it last.

Not proposed, and deliberately: no second ledger, no changes to the Scene /
Panel / Beat hierarchy, no changes to Gate 1, the quote binding, the cost table,
the lock handler or `plan_signature`'s material-field set.

---

## 14. Scope line — the answer, with one correction

The proposed line:

> Preflight applies to anything that becomes a prompt sent to fal — stills and
> video. It never applies to local renders.

**The second sentence is right and needs no defence.** `motion.render_shot` and
the parallax/depth path take no prompt and cost nothing; there is nothing to
predict and no spend to prevent.

**The first sentence is under-inclusive as written, and the gap is audio.** Three
more fal endpoints take a prompt and bill for it:

* SFX — `fal-ai/stable-audio` from `Shot.sfx`, per beat (audio.py:614)
* Music generation — `POST /api/music/generate` against `audio.MUSIC_BACKENDS`
  (main.py:3539)
* SFX layers — `POST /api/shot/{id}/layers/{id}/generate` (main.py:5161)

By the brief's own criterion these are in scope. **Recommend excluding them
anyway, for a stated reason rather than an oversight:** a text critic for an
audio prompt has no failure taxonomy in this repo, no measured reroll rate, no
cost model, and — unlike a still — the output is short, cheap and immediately
auditable by listening. Excluding them is a judgement about value, not about the
scope rule. Write it down as such so the next person does not have to re-derive
it.

**One narrowing that matters more than the audio question.** The proposed line
says "stills and video" as though those are one path. They are not, and §2.5
shows the split is where the money is:

* **Beat stills** (`assets._compose_prompt(Shot)`, 3 takes, re-rollable, $0.1194 a
  click) — this is where the 5–8 regenerations happen, and preflight here saves
  money.
* **Coverage stills** (`still_takes`, 1 take, *not* re-rollable) — preflight here
  saves nothing directly, because there is no reroll to prevent. It saves the
  shot, which is the only take that will ever exist.

Both should be in scope, but they are in scope for different reasons and their
success is measured differently. A slice that covers only the Director path will
look like it failed on the money metric while succeeding at the thing that
actually matters.

---

## 15. What this evaluation did not do

* Did not change any production code, add any test, or deploy anything.
* Did not read production telemetry: `prompt_ledger.jsonl` and `generation/`
  live on the GCS mount and are not present in this worktree. Every measurement
  here comes from committed artefacts — the four plans in `director_plans/`, the
  two real manifests, and the code.
* Did not query fal's billing API. The invoice figures in §2.1 and §2.3 —
  $0.0398/image, kling 20s $1.12, wan billed 6 units for a 4-unit request, the
  $3.20 account total — were obtained by another session with an admin key and
  are cited from `648f81b`'s committed provenance
  (`capabilities.py:237-254`, `:262-333`), not re-fetched here.
* Did not verify the fal tariff rows against fal's published pricing. That is
  `tests/test_fal_tariff.py`'s job and it is the thing keeping §2.3 honest, not
  this document.
* Did not measure tokens with `count_tokens`. §9 states the estimation method
  and its error bar — and, at the corrected still price, states where that error
  bar has stopped being comfortable.
* Did not invent a success probability for any shot, and did not propose one.

### What the price correction should be read as

The first draft of this document argued, from `COST_PER_IMAGE = 0.15`, that
preflight was so obviously cheap relative to the waste it prevented that the
estimate did not need to be precise. That was true of the constant and false of
the world. The lesson is the one `capabilities.py` already had written down about
the seedance row and has now had to write down twice: **a transcribed price is
not a measured one, and a conclusion whose margin comes from a transcription
inherits its error.**

What survives untouched is everything that was not derived from that constant —
§2.2's shot counts, §2.5's irreversibility finding, §3's inventory, and the
answers to A, B and C. What moved is every figure downstream of $0.15, and one
recommendation: the critic's model is no longer free to choose.
