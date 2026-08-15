# Antigravity — UI design brief

Written 2026-08-14. Supersedes `docs/antigravity_ui_handoff.md`, which scoped
Antigravity as a frontend implementer. **That role has changed.**

---

## Role

**You design. You do not code.**

Claude Code owns implementation, backend, API contracts, persistence and
deploy. Codex owns adversarial review. Neither of them proposes interaction
design, which is why you are here.

You produce specifications a builder can implement without inventing anything.
You do not open a pull request, and you do not iterate in rounds — a design
enters the queue as an input to a build slice, once.

### What a deliverable is

Not prose, and not a picture on its own. Each surface you design MUST state:

- **Component inventory** — what exists, and which existing component it
  replaces or extends. The current set is in `frontend/src/components/`.
- **Every state**, not just the happy one: empty, loading, partial, error,
  stale, permission-denied, and "the server has not confirmed this yet."
- **The data each surface needs**, field by field, and **which endpoint
  provides it**.
- **What a user can do from it**, and what each action calls.

If a surface needs data no endpoint returns, **say so and stop**. Do not design
around it, and do not assume a route. Missing data is a backend change, and
that request is cheap to make and expensive to work around.

---

## Invariants your design must respect

These are settled. A design that violates one is unbuildable here, and the
reason each exists is a bug that shipped.

**Rendering happens on the server. The browser never composites.** There is no
second compositor and there must not be one — it would drift from the real
renderer and cannot reproduce the depth-warp parallax, so any in-browser
preview is a confident lie about what ships. The timeline lays out what the
server *will* render; the video element plays what it *did*. If a design seems
to need in-browser compositing, the answer is a server render plus a job.

**The UI never claims state the server has not confirmed.** No optimistic
"saved", "approved", "generated" or "exported". This is contract §11.4 and it
has already been violated once: dismissing a critic warning changed React state
and nothing else, so the screen reported a clean review while the saved plan
still carried the finding.

**A placeholder carries identity, not just appearance.** A missing shot is a
slot that knows its shot id, its beat, its intended duration and the media type
it expects. Design it as a *pending shot*, never as a grey rectangle — the
whole point is that a later render drops into it without rebuilding the edit.

**Never hide a control; explain it.** A locked stage, an inert panel, a missing
clip — each says *why*. Controls that vanish read as breakage, and this UI has
lost controls twice that way.

**Distinguish "not generated" from "none for this beat."** They need different
actions, and collapsing them has already cost debugging time.

**Commit on release, not on change.** A range input fires per pixel of travel
and every write is a GCS round trip; one slider drag was ~100 writes. Design
interactions that settle on release.

**All state and handlers live in `page.tsx`.** Design presentation; assume the
builder extracts presentation only. Two surfaces that regenerate audio call the
same handler on purpose — "regenerate" must mean one thing wherever it is
pressed.

---

## First brief: the render queue and origin-aware navigation

This is first because Phase 1 already defines the data it binds to, so a design
now is buildable rather than speculative.

### The problem

Background generation is currently close to invisible, and what is visible is
wrong in specific ways:

- `JobBanners.tsx` recognises jobs by a **hardcoded chain** of names —
  `script_draft`, `drafts`, `render`, `motion_preview`, `narration`, `preview`.
  Any other kind runs with no indicator at all.
- **There is no way back.** A render belongs to a shot, in a beat, in a
  project, and nothing in the UI carries you there. Navigating away means
  losing the thread.
- **A vanished job reads as a stalled one.** `page.tsx:pollJobs` counts four
  consecutive misses before deciding a job died, because a cold start erases
  the registry. That is loss detection, not reconciliation.
- **No elapsed time, no provider, no model, no cost.** A render either is
  happening or is not.

### What Phase 1 will provide

Design against this; it is specified in `docs/vnext/phase1_spec.md` §9.2. Per
job:

```
job_id                 stable identity
logical_name           display key, e.g. "render"
state                  QUEUED | RUNNING | READY | FAILED | CANCELLED | RECONCILING
status                 legacy projection: running | done | error
dispatch_state         not_dispatched | dispatching | provider_accepted | outcome_unknown
reconciliation_state   not_needed | pending | checking | resolved | manual_required | failed
origin                 { scene_id, panel_id, beat_id, attempt_id, view }
provider               e.g. "fal"
created_at, updated_at, heartbeat_at
log                    bounded, sanitised, diagnostic only
snapshot_version       monotonic; a job absent from a snapshot is NOT gone
```

Scope: **the single authenticated studio tenant.** All active work belonging to
this studio, across projects. There is no per-user account model yet, so do not
design one; do not design anything that implies one user seeing another's work.

### Design these

1. **The persistent activity indicator.** Always visible, survives navigation.
   Shows active and queued counts. What it looks like at zero matters as much
   as at eight.
2. **The queue panel it opens.** Per job: what is being made, which project,
   which scene/panel/beat, provider, elapsed time, state, and a way back to the
   origin. Grouped by project, since work spans projects.
3. **Return-to-origin.** Clicking a job takes you to the thing it belongs to —
   the right project, the right stage, the right shot. Navigating away must
   never cancel work.
4. **The states that are not "running."** These are the ones that matter and
   the ones current UI has no vocabulary for:
   - `RECONCILING` — we are checking with the provider. Not failed. Not hung.
   - `manual_required` — a paid render whose outcome is genuinely unknown; the
     money may already be spent. This needs a human decision, and the design
     must make the stakes legible without being alarming. It cannot be a red
     error toast.
   - `outcome_unknown` after a crash, distinct from a clean failure.
   - `QUEUED` versus `RUNNING` — accepted but not started is not the same as
     working.
5. **A job that disappears from a snapshot.** With `snapshot_version`, the UI
   can tell "not in this response" from "gone." Design what the user sees while
   that resolves — the current answer is a four-miss guess.

### Do not design yet

Concept exploration, Creative DNA controls, the fourteen-stage navigation,
Quick Generate, entity/reference browsers. **No schema exists for any of them.**
Designing them now produces artifacts that get redrawn when the data model
lands, which is the same double-build trap that got V1 slice 6 deferred.

---

## Handing work back

A design lands as a document in `docs/design/`. Claude Code implements it in a
build slice; Codex reviews the implementation, not the design.

If implementation reveals the design assumed data that does not exist, that
comes back to you as a question — not as a builder improvising a substitute.

Verification of the built result is the builder's: `npx tsc --noEmit` and
`npm run build` must both pass clean, and deploy is Claude Code's alone.

### Also true

`frontend/AGENTS.md`: this Next.js differs from the version in most training
data. It matters for implementation, not for design, but it is the reason the
role split exists — confidently wrong framework idioms are expensive, and
design specifications do not carry that risk.
