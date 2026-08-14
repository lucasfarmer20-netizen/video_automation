# vNext Phase 1 — Durable Job Authority: normative implementation specification

**Status:** proposed, for adversarial review. Not approved for implementation.
**Repo baseline:** `main` @ `a5e57de` (see §2 for the facts this rests on).
**Supersedes:** the Phase-1 sketch in `docs/vnext/code_evaluation.md` §6, which
treated persistence as though it also provided execution recovery. It does not.

Normative keywords: **MUST**, **MUST NOT**, **SHOULD**, **MAY**.

Inputs this specification is written against:

- `docs/vnext/code_evaluation.md` — gap evaluation
- the Phase-1 adversarial architecture review (NO-GO as previously specified)
- the four locked architecture decisions (§1)
- `docs/audits/orchestration_guardrails.md` — review process

---

## 0. Scope

Phase 1 makes background work **durable, recoverable and attributable**. It
changes no creative behaviour.

**In scope:** job authority, typed commands, leases and fencing, Cloud Tasks
dispatch, provider submission and reconciliation, stable project identity,
internal-endpoint authentication, a compatibility projection for existing
callers, and bringing every paid render path it touches under
`GenerationAttempt`.

**Out of scope, and MUST NOT be changed by this phase:** Scene/Panel/Beat
renaming; the entity registry; prompt compilers or validation stamps; QC
findings; timeline redesign; model routing, prompts, durations, or cost
calculation; broad `main.py` decomposition; unrelated UI redesign.

**Behaviour that MUST remain identical:** script lock, storyboard approval and
paid gates; `GenerationAttempt` terminal immutability, signature reuse,
in-flight refusal and human abandonment; timeline slot identity, trims, active
media selection, preview and export; per-project `ProjectContext` request
scoping; existing manifest schema and per-project file locations.

---

## 1. Locked decisions

1. **Firestore is the transactional job control plane.** Project manifests,
   coverage plans, generation ledgers and timeline slots stay as files where
   they are. Firestore holds only jobs, leases, dispatch state, provider
   locators, reconciliation state and the project-identity index.
2. **The Firestore emulator is mandatory** for local development and CI. Lease,
   fencing and concurrency tests MUST run against the emulator, not a fake.
3. **Cloud Tasks invokes authenticated internal endpoints** with OIDC audience
   verification.
4. **fal webhook signatures MUST be verified**, and reconciliation MUST also be
   Cloud Tasks-driven; a webhook is an optimisation, never the only path.
5. **Project identity is a newly minted persistent UUID.** Path-derived IDs are
   retained as aliases only.
6. **Phase 1A** delivers UUID migration, job infrastructure, security,
   leases/fencing, typed commands and the compatibility projection.
7. **Phase 1B** migrates all paid fal paths — explicitly including
   `/api/shot/{scene_id}/generate_video` — under `GenerationAttempt`.
8. **Global activity is scoped to the single authenticated studio tenant**
   unless and until account identity is introduced separately.
9. **No blanket stale-job replay.** Each job kind carries an explicit recovery
   policy.

---

## 2. Verified repository baseline

Facts the specification depends on, verified in the working tree. A reviewer
should re-verify these before challenging anything built on them.

| Fact | Evidence |
|---|---|
| Job registry is in-memory, keyed `(project_id, name)` | `pipeline_worker.py:25` `_jobs` |
| `start_job(name, fn, ctx=None) -> bool`; `False` when that kind is already running | `pipeline_worker.start_job` |
| `get_jobs_status()` is project-scoped and returns `{name: {status, log, project_id}}` | `pipeline_worker.get_jobs_status` |
| Frontend consumes a map keyed by logical name with lowercase `running`/`done`/`error` | `page.tsx:pollJobs`, `JobBanners`, `AssemblyPanel`, `directorApi.waitForJob` |
| `Storyboard.id` is path-derived and rewritten on load/save at five sites | `main.py:93,276,297,331,1373` |
| The persisted manifest currently stores that derived value as `id` | `bestiary/manananggal/storyboard_manifest.json` |
| `Storyboard.from_dict(project_id, data, shots_list)` takes identity as a parameter | `manifest.py:268` |
| Auth middleware checks `X-Studio-Key` **only on non-GET**; media paths are public | `main.py:133-142`, `_PUBLIC_PATHS` |
| Firestore client already imported; `db is None` when unavailable | `manifest.py:18-23` |
| `fal_client` exposes `submit`, `status`, `result`, `cancel` | installed client |
| Paid render currently reached from `director._compile_locked` only | `director.py:1326` |
| `/api/shot/{scene_id}/generate_video` spends without creating an attempt | `main.py:3583` |
| Job kinds in use | `script_draft, narration, sfx, music, rough_cut, metadata, bundle, transcode, casting, motion_preview, spike_identity, director:{beat}` |
| Deployment: GCS volume, min-instances 0, max-instances 1 | `cloudbuild.yaml` |

---

## 3. Project identity

### 3.1 Rules

- Every project MUST have a `project_uuid`: a UUIDv4 minted once, persisted in
  the manifest, and **never derived from anything**.
- `project_uuid` is identity. The filesystem path is location. Move, trash,
  restore and rename MUST NOT change it.
- The five sites that assign `sb.id` from a path (§2) MUST stop doing so.
  `Storyboard.from_dict` MUST read a persisted id when present.
- The legacy path-derived id MUST be retained as an **alias**, so existing
  Firestore documents and any stored reference still resolve.

### 3.2 Alias index

Firestore collection `project_aliases`:

```
{alias_id}                     # the legacy path-derived id, doc id
  project_uuid: string
  first_seen_at: timestamp
  source: "migration" | "path-change"
```

Resolution order for an inbound request: explicit `project_uuid` → alias lookup
→ path derivation (deprecated, logged). Path derivation MUST be removed as a
resolution route before Phase 2.

### 3.3 Migration

Migrate on read, idempotently, per the standing project constraint:

1. On load, if the manifest has no `project_uuid`, mint one, write it, and
   record the current path-derived id in `project_aliases`.
2. Provenance MUST be recorded: `project_uuid_source: "migrated"` plus the
   alias it was migrated from.
3. A migration MUST NOT be inferred for a project that cannot be read. An
   unreadable manifest fails closed; it does not receive a new identity.
4. Two projects MUST NOT be able to acquire the same `project_uuid`. Alias
   collisions MUST fail loudly rather than merge.

---

## 4. Firestore data model

Collections. All documents are small; media and manifests remain on disk.

### 4.1 `jobs/{job_id}`

```
id                    string   immutable UUID; the only identity
project_uuid          string   stable project identity (§3)
kind                  string   typed command kind (§8)
logical_name          string   compatibility/display key, e.g. "render"
state                 enum     QUEUED|RUNNING|READY|FAILED|CANCELLED|RECONCILING
dispatch_state        enum     not_dispatched|dispatching|provider_accepted|outcome_unknown
reconciliation_state  enum     not_needed|pending|checking|resolved|manual_required|failed
origin                map      {schema_version, scene_id, panel_id, beat_id, attempt_id, view}
command_version       int
command_payload       map      typed, minimal, no callables, no credentials
idempotency_key       string   request identity; unique per (project_uuid, kind, key)
input_signature       string   links to immutable generation-input identity
attempt_id            string   REQUIRED for paid generation jobs
provider              string   e.g. "fal"
provider_job_id       string   set only after provider acceptance
provider_idempotency_key string
lease_owner           string
lease_token           int      monotonic; fencing token
lease_expires_at      timestamp
version               int      optimistic concurrency; incremented every write
run_count             int
recovery_count        int
created_at            timestamp
updated_at            timestamp
heartbeat_at          timestamp
dispatched_at         timestamp
cancel_requested_at   timestamp
terminal_reason       enum     completed|provider_failure|cancelled|abandoned|orphaned|superseded
result_ref            map      {kind: "generation_attempt", id} — reference, never duplicated truth
expires_at            timestamp retention
error                 map      {code, public_message}
```

`log` is **not** stored here. Logs are a bounded, sanitised projection
(§9.3); they are never execution authority.

### 4.2 `job_idempotency/{project_uuid}:{kind}:{idempotency_key}`

Doc id enforces uniqueness. Holds `{job_id, created_at}`. Created in the **same
transaction** as the job (§5.1).

### 4.3 `project_aliases/{alias_id}` — §3.2.

---

## 5. Transaction boundaries

Every rule below is a Firestore transaction. Anything not listed MUST NOT be
transactional, to keep contention low.

### 5.1 Accept a request

One transaction, and it MUST contain all of:

1. read `job_idempotency/{...}`; if present, return the existing `job_id` and
   create nothing;
2. create `jobs/{job_id}` with `state=QUEUED`, `dispatch_state=not_dispatched`,
   `version=1`;
3. create the idempotency doc.

The API **MUST NOT** report a request as started until this transaction commits.
Cloud Task enqueue happens **after** commit; a failed enqueue leaves a QUEUED
job that reconciliation will pick up, which is safe. Enqueueing before commit is
forbidden — it can dispatch a job that does not exist.

### 5.2 Acquire a lease

One transaction: read the job; proceed only if
`state ∈ {QUEUED, RECONCILING}` **or** (`state == RUNNING` and
`lease_expires_at < now`); then set `lease_owner`, `lease_token = previous + 1`,
`lease_expires_at = now + LEASE_TTL`, `state = RUNNING`, `version += 1`,
`run_count += 1`.

`lease_token` MUST be monotonic. Every subsequent write by that worker MUST be
conditioned on both `lease_token` and `version`. A worker whose condition fails
MUST stop and MUST NOT write.

### 5.3 Record provider acceptance

One transaction, conditioned on the fencing token: set `provider_job_id`,
`dispatch_state = provider_accepted`, `dispatched_at`, `version += 1`.

**Ordering rule:** `dispatch_state` MUST be set to `dispatching` and committed
*before* the provider call is made. A crash between that commit and provider
acceptance therefore lands in `dispatching`, which §7.4 treats as
`outcome_unknown`, never as `not_dispatched`.

### 5.4 Terminal transition

One transaction: reject if the job is already terminal and the incoming
outcome differs in any field (mirrors `generation.TerminalConflict`). An exact
replay is a no-op. Terminal states are monotonic.

### 5.5 Ordering against `GenerationAttempt`

The attempt is the authority for render truth; the job references it. Therefore:

1. attempt created (existing `generation.begin`) — **before** any provider call;
2. job `dispatch_state = dispatching` committed;
3. provider call;
4. provider id recorded;
5. attempt finalised (`generation.succeed` / `fail`);
6. **only then** job `state = READY`.

A job MUST NOT be `READY` before its attempt transition is durably written. If
step 5 fails, the job stays `RECONCILING` — never `FAILED`, because the media
may exist.

---

## 6. State machines

### 6.1 Job state

```
QUEUED ──lease──► RUNNING ──attempt finalised──► READY
   │                 │
   │                 ├── provider failure ─────► FAILED
   │                 ├── lease expired ───────► RECONCILING
   │                 └── cancel requested ────► CANCELLED
   │
   └── cancel before dispatch ─────────────────► CANCELLED

RECONCILING ──provider says done + attempt written──► READY
            ──provider says failed────────────────► FAILED
            ──no provider evidence available──────► RECONCILING
                                                    (reconciliation_state=manual_required)
```

`READY`, `FAILED` and `CANCELLED` are terminal. **Lease expiry alone MUST NOT
produce `FAILED`, and MUST NOT authorise redispatch** (invariant 7 of the
review). Time is not evidence.

### 6.2 Dispatch state

```
not_dispatched ──► dispatching ──► provider_accepted
                        │
                        └──► outcome_unknown   (crash or ambiguous response)
```

Only `not_dispatched` permits a first paid dispatch. `outcome_unknown` MUST be
resolved by provider query or by human decision before any further paid call.

### 6.3 Reconciliation state

`not_needed → pending → checking → resolved | manual_required | failed`.
`checking` MUST be visible to clients (§9.2); a job MUST NOT disappear from a
snapshot while being reconciled.

---

## 7. Execution and endpoints

### 7.1 Flow

```
request → §5.1 transaction → Cloud Task enqueue
   → POST /internal/jobs/{id}/dispatch
   → §5.2 lease → attempt (paid) → §5.3 dispatching
   → fal_client.submit(...) → record provider_job_id → return 2xx promptly
   → fal executes independently
   → webhook  ─┐
               ├─► POST /internal/jobs/{id}/reconcile → status/result → finalise
   → Cloud Tasks scheduled reconcile ─┘
```

A dispatch handler MUST return promptly and MUST NOT block on generation.
Reconciliation MUST NOT be driven by an in-process timer: `min-instances=0`
means the instance can be reclaimed, so the wake-up MUST come from Cloud Tasks
or a webhook.

### 7.2 Internal endpoints

`POST /internal/jobs/{job_id}/dispatch` and
`POST /internal/jobs/{job_id}/reconcile`.

- Both MUST require a Google-signed OIDC token from the Cloud Tasks service
  account, with the **audience verified** against the endpoint's own URL, and
  the issuer and service-account email checked against an allowlist.
- Both MUST be excluded from `_PUBLIC_PATHS` and MUST NOT be reachable with only
  `X-Studio-Key`.
- The existing middleware (`main.py:136`) checks `X-Studio-Key` only on non-GET;
  these endpoints MUST NOT inherit that rule. **An unauthenticated internal
  dispatch endpoint is a route for an anonymous caller to spend money**, and the
  service currently allows unauthenticated invocation.
- Both MUST be idempotent: repeated delivery of the same Cloud Task MUST NOT
  produce a second provider call.

`POST /internal/webhooks/fal`

- The signature MUST be verified according to fal's published webhook scheme.
  An unverified webhook MUST be rejected and MUST NOT be treated as evidence of
  completion.
- A verified webhook is an accelerator only. Reconciliation MUST still be
  scheduled, so a lost or unverifiable webhook cannot strand a job.

### 7.3 Provider interface

`fal_client.submit` MUST be used for paid work; `subscribe` MUST NOT be used on
any path that a job owns, because it ties the outcome to process lifetime and
returns no durable locator. `provider_idempotency_key` MUST be sent where fal
supports it.

### 7.4 Recovery decision table

| `dispatch_state` | Permitted recovery |
|---|---|
| `not_dispatched` | dispatch once, under a valid lease |
| `dispatching` | treat as `outcome_unknown`; query provider; **no automatic redispatch** |
| `provider_accepted` | query `status`/`result` only |
| `outcome_unknown` | query if a locator exists; otherwise `manual_required` |

Where a model offers no status or idempotency facility, the job MUST enter
`manual_required`. Automated redispatch is forbidden.

---

## 8. Typed commands and recovery policy

Every job kind MUST register: a payload schema, a handler, and a recovery
policy. There is **no blanket "stale means rerun."**

| Kind | Paid | Recovery policy |
|---|---|---|
| `director:{beat}` (coverage compile) | yes | provider reconciliation; never redispatch on lease expiry alone |
| `render` / `generate_video` (1B) | yes | as above |
| `script_draft` | LLM cost | replayable; idempotency key prevents duplicate writes |
| `narration`, `sfx`, `music` | provider cost | replayable **only** where the output path is content-addressed; otherwise reconcile |
| `rough_cut`, `motion_preview`, `transcode`, `bundle` | local compute | freely replayable; must be safe to re-run over partial output |
| `metadata`, `casting`, `spike_identity` | mixed | replayable; MUST NOT overwrite a human selection |

A kind without a registered recovery policy MUST NOT be accepted by the job
system.

---

## 9. Compatibility

### 9.1 `start_job` façade

`start_job(name, fn, ctx=None) -> bool` MUST keep its signature and its
duplicate-start semantics — `False` when a job of that kind is already active
for that project — until callers migrate. Internally it creates a durable job.
Kinds not yet migrated to typed commands MAY continue to execute in-process
behind the façade, provided they still create a durable record.

### 9.2 `/api/assemble/status`

MUST remain project-scoped, MUST keep returning `project_id`, and MUST keep
returning a map keyed by logical name with lowercase `status` values, because
`JobBanners`, `AssemblyPanel` and `waitForJob` consume exactly that shape.

Projection:

| Durable `state` | Legacy `status` |
|---|---|
| `QUEUED`, `RUNNING`, `RECONCILING` | `running` |
| `READY` | `done` |
| `FAILED`, `CANCELLED` | `error` |

New fields (`job_id`, `state`, `dispatch_state`, `reconciliation_state`,
`origin`) MUST be added **alongside**, never replacing. The response MUST carry
a monotonically increasing `snapshot_version`, and a client MUST NOT delete a
locally known live job on the basis of a snapshot that omits it unless that
snapshot carries an explicit tombstone. This removes the four-miss workaround in
`page.tsx:pollJobs`, which is loss detection rather than reconciliation.

### 9.3 Logs

Bounded and sanitised, stored outside the job document, and served through the
projection. Logs MUST NOT carry credentials or signed URLs, and MUST NOT be
read as state.

---

## 10. Phase 1A — infrastructure

**Delivers:** project UUID migration and alias index; Firestore job repository
with the transactions in §5; typed command registry with recovery policies;
leases and fencing; Cloud Tasks dispatch with OIDC-verified internal endpoints;
the compatibility projection; the global activity read model scoped to the
single studio tenant.

**Does not deliver:** any change to paid generation paths. Existing behaviour
continues to run behind the façade.

### Acceptance tests

Each MUST be mutation-sensitive: a faithful mutation of the mechanism MUST fail
the test (`docs/audits/orchestration_guardrails.md`).

1. **Durable acceptance** — the API cannot report started unless the job is
   readable after a simulated process restart. *Mutation: persist after thread
   start → fails.*
2. **Restart survival** — QUEUED, RUNNING, terminal and RECONCILING jobs all
   survive recreation of the repository and service objects.
3. **Lease exclusion** — two workers race; exactly one acquires. Emulator, real
   transactions, multiple processes.
4. **Fencing** — a worker holding an expired lease cannot write after a newer
   token exists. *Mutation: drop the token condition → fails.*
5. **Lease expiry is not failure** — an expired lease yields `RECONCILING`,
   never `FAILED`, and never a redispatch.
6. **Idempotent acceptance** — a replayed HTTP request returns the same
   `job_id` and creates one job.
7. **Enqueue ordering** — a failure to enqueue leaves a QUEUED job that
   reconciliation picks up; no job is ever dispatched before its record commits.
8. **OIDC** — internal endpoints reject a missing token, a wrong audience, a
   wrong issuer and a non-allowlisted service account. *Mutation: skip audience
   check → fails.*
9. **`X-Studio-Key` is not sufficient** for internal endpoints.
10. **Project identity** — move a project on disk; the job still resolves to the
    same `project_uuid`. Trash and restore likewise.
11. **Alias resolution** — a legacy path-derived id resolves; a colliding alias
    fails loudly.
12. **Migration idempotence** — repeated loads mint exactly one UUID and write
    once.
13. **Isolation** — the same kind runs in A and B; status, logs, origins and
    navigation never cross.
14. **Legacy projection** — existing routes and components consume the status
    shape unchanged; `waitForJob` still resolves.
15. **Snapshot integrity** — a partial snapshot cannot erase a locally known
    live job without a tombstone.
16. **Emulator required** — the concurrency suite fails loudly if the emulator
    is absent rather than silently degrading to a fake.

---

## 11. Phase 1B — paid paths under `GenerationAttempt`

**Delivers:** every paid fal path that a job owns creates an attempt before
dispatch, uses `fal_client.submit`, persists the provider locator, and
reconciles. Explicitly includes `/api/shot/{scene_id}/generate_video`, which
today spends without an attempt and therefore has none of the existing
protections.

**Constraint:** prompts, model selection, durations, cost calculation, download
and placement behaviour, and selected-attempt semantics MUST NOT change.

### Acceptance tests

1. **Universality** — no paid fal call is reachable without an attempt.
   *Enforced by a test that greps the dispatch paths and by behavioural tests
   on each route.*
2. **`generate_video` under the invariant** — duplicate requests produce one
   attempt and one provider call; the in-flight guard applies; spend is
   recorded.
3. **Crash windows** — kill before attempt, after attempt/before dispatch,
   during dispatch, after provider acceptance, after download, before attempt
   finalisation, before job terminal. Each yields the state in §7.4 and never a
   second charge.
4. **Offline completion** — the provider completes while the service is down;
   reconciliation finalises without a second provider call.
5. **Unknown outcome** — no locator and no status facility ⇒ `manual_required`;
   automated retry refused.
6. **Terminal conflict** — a late failure cannot rewrite a success, and vice
   versa, at both job and attempt level.
7. **Attempt-write failure** — job does not claim `READY`; state remains
   reconcilable.
8. **No behavioural drift** — the existing generation suites
   (`test_generation_lineage.py`, `test_paid_rebill.py`) pass unchanged.

---

## 12. Rollback boundaries

- **1A is reversible.** A feature flag (`JOBS_BACKEND=memory|firestore`) selects
  the repository. The façade keeps callers identical, so reverting the flag
  restores current behaviour. Durable records left behind are inert.
- **The UUID migration is additive and not reversible in place.** It only adds
  a field and an alias row; the legacy id keeps resolving, so a rollback of code
  does not strand data. It MUST therefore ship before, and separately from, the
  job cutover.
- **1B is reversible per route.** Each migrated route keeps its pre-migration
  code path behind a flag until its acceptance tests have run in the deployed
  environment.
- **Irreversible once live:** provider `request_id`s recorded against attempts.
  That is intended — it is the evidence reconciliation depends on.

---

## 13. Test infrastructure

- The Firestore emulator MUST be available locally and in CI. Concurrency,
  lease, fencing and uniqueness tests MUST run against it.
- A fake/in-memory repository MAY exist for unit tests of unrelated logic, but
  MUST NOT be used for any test asserting transactional behaviour — a fake would
  pass while proving nothing, which is the failure mode this project has already
  hit four times.
- Test collection MUST be fixed first: bare `pytest` currently fails with four
  collection errors from `scratch/`, so "the suite passes" depends on
  invocation.

---

## 14. Open items for the reviewer

1. fal's exact webhook signature scheme is not restated here; the requirement is
   normative, the mechanism must be confirmed against fal's documentation at
   implementation time.
2. Whether `narration`/`sfx`/`music` outputs are content-addressed determines
   whether they are freely replayable (§8). Unverified.
3. Retention: `expires_at` policy for job documents and logs is unspecified.
4. Whether Cloud Tasks or Cloud Scheduler drives periodic reconciliation sweeps
   for jobs with no webhook.
5. Whether 1A should also migrate `director:{beat}` to typed commands, or leave
   it on the façade until 1B.
