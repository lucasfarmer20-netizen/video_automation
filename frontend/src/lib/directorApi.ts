import {
  DirectorCoveragePlan,
  DirectorShot,
  DirectorWarning,
  SceneFinding,
  SceneSummary,
  CreativePreferences,
  DirectorProfilesResponse,
  CoverageSurvey,
  WarningDisposition,
} from "../types/director";

const API_BASE =
  typeof window !== "undefined"
    ? window.location.hostname === "localhost"
      ? "http://localhost:5000"
      : ""
    : "";

/**
 * Helper to include X-Studio-Key header on all mutating (POST) requests
 * Read from localStorage.studioKey (matching page.tsx authHeaders)
 */
export function getAuthHeaders(base: Record<string, string> = {}): Record<string, string> {
  const headers: Record<string, string> = { ...base };
  if (typeof window !== "undefined") {
    const key = window.localStorage.getItem("studioKey") || "";
    if (key) {
      headers["X-Studio-Key"] = key;
    }
  }
  if (activeProjectId) {
    headers["X-Project-Id"] = activeProjectId;
  }
  return headers;
}

/**
 * The project every director call targets (contract §11.3).
 *
 * Director endpoints read and write coverage plans, which live inside one
 * project's directory. Sending the id makes each request name its target rather
 * than inheriting whatever the shared active-project pointer happens to say —
 * so a project switch mid-flight cannot land a plan in the wrong film.
 */
let activeProjectId = "";

export function setActiveProjectId(id: string): void {
  activeProjectId = id || "";
}

/** Whether a reply describes a project we have since navigated away from. */
export function isStaleReply(res: Response): boolean {
  const said = res.headers.get("X-Project-Id");
  return Boolean(said && activeProjectId && said !== activeProjectId);
}

/**
 * GET /api/director/profiles
 * Authoritative profiles, vocabulary, and video_capabilities
 */
export async function fetchDirectorProfiles(): Promise<DirectorProfilesResponse> {
  const res = await fetch(`${API_BASE}/api/director/profiles`, { headers: getAuthHeaders() });
  if (!res.ok) {
    throw new Error(`Failed to fetch director profiles: ${res.status} ${res.statusText}`);
  }
  const data = await res.json();
  if (!data.ok) {
    throw new Error(data.error || "Failed to load director profiles");
  }
  return data;
}

/**
 * Every key a plan arrives with, and what this mapper does with it.
 *
 * `GET /api/director/scene` serialises `director.CoveragePlan` with `asdict`, so
 * the keys below ARE that dataclass. The mapper does not spread them: it builds
 * a flat `DirectorCoveragePlan` field by field, because the wire shape is a
 * `{beats: [{plan, beat_duration, coverage_total}]}` envelope and several fields
 * are answered by the scene summary rather than by the plan (see `estimated_cost`
 * below, where the difference is a price the human is asked to agree to).
 *
 * That normalisation is legitimate. A hand-written copy that must be edited
 * every time the server grows a field is not — it loses fields silently, and it
 * already did: `warning_dispositions` was written, persisted and returned by the
 * backend, and dropped here, so six findings a human had resolved came back on
 * every scene switch. The write had been fixed; the read still threw it away.
 *
 * So the two records below are the mapper's account of the whole dataclass, in
 * the same discipline as `_MATERIAL_SHOT_FIELDS` / `_NON_MATERIAL_SHOT_FIELDS`
 * in `backend/director.py`: dropping a field is a decision on the record, never
 * an omission. `tests/test_director_scene_mapping.py` compares both records
 * against `CoveragePlan`'s real fields and fails when the server grows a key
 * that appears in neither — which is the only place that can see both sides.
 * `directorApi.dispositions.test.ts` then checks that everything CARRIED
 * actually arrives, so a name here cannot stand in for a line of code.
 */
export const PLAN_FIELDS_CARRIED: Record<string, string> = {
  beat_id: "beat_id",
  beat_duration: "beat_duration",
  version: "version",
  plan_id: "plan_id",
  scene_beats: "scene_beats",
  status: "status",
  profile: "profile",
  created_by: "created_by",
  coverage: "coverage",
  warnings: "warnings",
  warning_dispositions: "warning_dispositions",
  compiled: "compiled",
  approved_signature: "approved_signature",
  visual_strategy: "visual_strategy",
  blocking: "blocking",
};

/** Plan keys deliberately not carried, each with the reason it is not needed. */
export const PLAN_FIELDS_DROPPED: Record<string, string> = {
  beat_signature:
    "the baseline the SERVER compares a beat against; it answers staleness itself " +
    "on the compile refusal (`stale`), and a client comparison would be a second " +
    "opinion about a decision that is not the client's",
  approved_at:
    "the approval TIMESTAMP, and it is cleared the moment it would be worth " +
    "showing: `invalidate_approval` blanks it when a plan drifts, so a drifted " +
    "plan carries an empty string. The old value survives in approval_history, " +
    "server-side, which is where an explanation of drift would have to come from",
  approved_by:
    "every call site is `approve(plan, beat=beat)`, so this is the constant " +
    "\"human\" on every plan in a single-tenant studio. Rendering it would be " +
    "theatre — it names nobody",
  approval_history:
    "audit record of superseded signatures. The compile route already reads it " +
    "SERVER-side and answers `approval_drifted`, which is what tells 'never " +
    "locked' apart from 'locked, then edited' — the client does not need the " +
    "record to classify the refusal. Showing the human WHICH approval they lost " +
    "and when is a real feature, and a feature is not a fix-round change",
};

/** One entry of `GET /api/director/scene`'s `beats[]`, as far as this file reads it. */
type SceneBeatEntry = {
  beat_id?: string;
  beat_duration?: number;
  coverage_total?: number;
  plan?: {
    beat_id?: string;
    scene_beats?: string[];
    warnings?: DirectorWarning[];
    warning_dispositions?: Record<string, WarningDisposition>;
    [k: string]: unknown;
  } | null;
};

/**
 * Every finding in a scene, keyed the way the lock keys them.
 *
 * `POST /api/director/lock_scene` refuses if ANY beat in `beats[]` still holds a
 * finding nobody decided, so the set a human has to work through is the union
 * across the scene — not the selected beat's slice of it. Collapsing by warning
 * id is not a nicety here: `director.warning_id` hashes the finding's content,
 * and `critique` stores a finding whose own `beat_id` is "" onto every beat it
 * was asked about, so one cross-beat finding arrives as N identical copies with
 * one shared id. Rendering it N times would be a different lie from the one
 * being fixed, and deciding one copy would leave the rest refusing the lock.
 *
 * A copy with no recorded decision makes the whole finding undecided. That is
 * the safe direction and it is also the true one: the beat holding the
 * undecided copy is a beat `lock_scene` will refuse on.
 *
 * Findings that arrived without an id (the critique reply returns raw warnings,
 * un-normalised) cannot be collapsed and cannot be decided; they are kept
 * distinct per beat so they are at least COUNTED, since they do still block.
 */
export function collectSceneFindings(entries: SceneBeatEntry[]): SceneFinding[] {
  const order: string[] = [];
  const byKey = new Map<string, SceneFinding>();
  for (const entry of entries || []) {
    const plan = entry?.plan;
    if (!plan) continue;
    const beatId = String(plan.beat_id || entry.beat_id || "");
    const dispositions = plan.warning_dispositions || {};
    (plan.warnings || []).forEach((warning, index) => {
      const key = warning?.id || `${beatId}#${index}`;
      const decided = warning?.id ? dispositions[warning.id] : undefined;
      const seen = byKey.get(key);
      if (seen) {
        if (!seen.beats.includes(beatId)) seen.beats.push(beatId);
        if (!decided?.decision) {
          seen.decision = "";
          seen.note = undefined;
        }
        return;
      }
      order.push(key);
      byKey.set(key, {
        id: warning?.id || "",
        beats: [beatId],
        warning,
        decision: decided?.decision || "",
        note: decided?.note,
      });
    });
  }
  return order.map((k) => byKey.get(k) as SceneFinding);
}

/**
 * GET /api/director/scene?beats=s004,s005,s006
 * Read model for a scene or set of beats (unauthenticated reads)
 */
async function readSceneBeats(beatsParam: string, tierFilter?: "needs_review") {
  let url = `${API_BASE}/api/director/scene?beats=${encodeURIComponent(beatsParam)}`;
  if (tierFilter) {
    url += `&tier=${tierFilter}`;
  }

  const res = await fetch(url, { headers: getAuthHeaders() });
  if (!res.ok) {
    throw new Error(`Director scene endpoint returned ${res.status} ${res.statusText} for beats=${beatsParam}`);
  }

  const data = await res.json();
  if (!data.ok) {
    throw new Error(data.error || `Failed to fetch scene coverage for beats=${beatsParam}`);
  }
  return data;
}

/** Whichever id a scene entry is filed under; the route always sends `beat_id`. */
function entryBeatId(entry: SceneBeatEntry): string {
  return String(entry?.beat_id || entry?.plan?.beat_id || "");
}

export async function fetchCoveragePlan(
  beatIds: string | string[],
  tierFilter?: "needs_review"
): Promise<DirectorCoveragePlan> {
  const beatsParam = Array.isArray(beatIds) ? beatIds.join(",") : beatIds;
  const data = await readSceneBeats(beatsParam, tierFilter);

  // If backend returns beat array shape
  if (data.beats && data.beats.length > 0) {
    const firstBeatWithPlan = data.beats.find((b: any) => b.plan !== null) || data.beats[0];
    if (firstBeatWithPlan && firstBeatWithPlan.plan) {
      const plan = firstBeatWithPlan.plan;
      const sceneBeats: string[] =
        plan.scene_beats && plan.scene_beats.length > 0
          ? plan.scene_beats
          : Array.isArray(beatIds)
            ? beatIds
            : [beatIds];

      // The scene's OWN findings, which is a wider set than this read asked for.
      //
      // The workspace mounts on one beat and this endpoint answers about exactly
      // the beats it was given, so a three-beat scene opened on its first beat
      // came back holding one beat's warnings — while `lock_scene` gates on all
      // three. The plan that just arrived is what says how wide the scene is, so
      // the remaining beats are read here rather than at each of the six call
      // sites, and ONLY their findings are taken from that second read: summary,
      // coverage and cost still describe the beats the caller asked about, and
      // must not silently widen to the scene under a call that did not.
      //
      // A failed second read leaves `findings_scope` short of `scene_beats`, and
      // that is the point of recording it — a reader that cannot see a beat has
      // to say so rather than count what it can see and call it the scene.
      const entries: SceneBeatEntry[] = [...data.beats];
      const missing = sceneBeats.filter(
        (b) => b && !entries.some((e) => entryBeatId(e) === b)
      );
      if (missing.length > 0) {
        try {
          const rest = await readSceneBeats(missing.join(","), tierFilter);
          for (const entry of rest.beats || []) entries.push(entry);
        } catch {
          // Deliberately swallowed: the beat this call was FOR did arrive, and
          // refusing to render it because a sibling read failed would take the
          // whole workspace away over a widening the caller never requested.
          // What is not swallowed is the consequence — see `findings_scope`.
        }
      }
      const examined = entries.map(entryBeatId).filter(Boolean);
      const findingsScope = [
        ...sceneBeats.filter((b) => examined.includes(b)),
        ...examined.filter((b) => !sceneBeats.includes(b)),
      ];

      return {
        scene_findings: collectSceneFindings(entries),
        findings_scope: findingsScope,
        plan_id: plan.plan_id || `plan_${beatsParam}`,
        // The beat this plan is FOR, which `scene_id` below is not: that is the
        // set of beats the read asked for. A two-beat scene returns one beat's
        // plan, so anything belonging to that beat alone — its compile record
        // most of all — has to be attributed to it and not to the whole scene.
        beat_id: plan.beat_id || firstBeatWithPlan.beat_id || "",
        scene_id: beatsParam,
        scene_title: `Scene ${beatsParam}`,
        scene_beats: plan.scene_beats || (Array.isArray(beatIds) ? beatIds : [beatIds]),
        status: plan.status || "draft",
        total_duration: firstBeatWithPlan.beat_duration || plan.total_duration || 0,
        beat_duration: plan.beat_duration,
        live_beat_duration: firstBeatWithPlan.beat_duration,
        coverage_total: firstBeatWithPlan.coverage_total,
        profile: plan.profile || "historical_docudrama",
        visual_strategy: plan.visual_strategy,
        blocking: plan.blocking,
        triage: data.triage || plan.triage,
        created_by: plan.created_by,
        version: plan.version,
        coverage: plan.coverage || [],
        warnings: plan.warnings || [],
        // The human's decisions about those findings, which is what makes the
        // list above readable. Without this the plan came back carrying six
        // findings and no record that any of them had been decided, so every
        // refetch — and a scene switch is always a refetch — re-raised findings
        // the human had already resolved and locked the scene on.
        //
        // `decideDirectorWarning`'s comment describes exactly this defect one
        // layer up: "resolve" used to filter the warning out of React state and
        // nothing else, so a refresh brought it back. The write was made durable
        // and the READ still discarded it, which is the same defect surviving
        // its own fix. The direction of the error is the safe one — more
        // findings than exist, never fewer — but a screen that forgets a review
        // is not one a human can be asked to trust when it says a plan is clean.
        //
        // Defaulted to {} rather than left undefined: every reader treats "no
        // entry" as undecided (contract §5.4), and an absent map and an empty
        // one must mean the same thing to them.
        warning_dispositions: plan.warning_dispositions || {},
        // What the last compile actually produced, as the server recorded it:
        // the beat clip, its runtime, its shot count. `status` says a compile
        // happened; this says what came out of it, and it is the only durable
        // account of that anywhere in the client.
        //
        // Dropping it is the second half of the same defect. The Director's
        // statement that a scene had compiled lived entirely in `compileDone`,
        // one-shot React state set when the job returned, so leaving the scene
        // erased the only copy — while the timeline proxy, which reads the
        // project directly and never comes through here, went on showing it.
        // The screen was not out of date; it had never been told.
        compiled: plan.compiled || {},
        // The identity of the plan the human approved, carried through so a
        // later compile can name WHICH plan it was quoted for. §11.5 keeps this
        // honest: a plan that mutates after approval loses the approval in
        // `load_plan` and comes back `draft`, so a plan that reads `locked`
        // always carries the signature of the coverage actually on screen.
        approved_signature: plan.approved_signature || "",
        // `??`, not `||`. A scene of nothing but parallax and static shots costs
        // 0.00, which is falsy — so `||` skipped the server's own answer and fell
        // through to the plan's stored `estimated_cost`, a value computed when
        // the plan was written rather than for the beats being asked about. A
        // free scene could therefore be quoted at a stale non-zero price in the
        // compile gate. The summary is the authority when it is present; the
        // fallbacks apply only when it is absent.
        estimated_cost: data.summary?.estimated_cost ?? plan.estimated_cost ?? 0,
        // What compiling this scene will BUY, as the server counts it. The
        // summary spans every beat in `beats`, which is exactly the set the
        // compile control sends. Falling back to counting this beat's own
        // ai_video shots is a floor, never an overstatement.
        paid_shots:
          data.summary?.paid_shots ??
          (plan.coverage || []).filter(
            (s: { motion_type?: string }) => s.motion_type === "ai_video"
          ).length,
      };
    }
  }

  // Direct plan object format fallback
  if (data.plan) {
    return data.plan;
  }

  throw new Error(`No coverage plan found for beats=${beatsParam} (404 / Unplanned)`);
}

/**
 * POST /api/director/plan
 * Request creative redirection or scene coverage re-planning (Requires X-Studio-Key auth)
 */
export async function redirectSceneCoverage(
  beats: string | string[],
  commandText: string,
  quickShortcuts: string[] = [],
  preferences?: CreativePreferences,
  profile?: string,
  // Plan over coverage the server would otherwise protect. Defaulted to false
  // and never inferred: the planner refuses locked beats precisely because that
  // coverage was reviewed and locked on purpose, and a client that quietly
  // retried with replan=true would turn a guard into a formality. Only a user
  // who has been told what it discards may set this.
  replan = false
): Promise<{ ok: boolean; job?: string; started?: boolean; error?: string }> {
  const beatList = Array.isArray(beats) ? beats : beats.split(",");
  const combinedNotes = [
    commandText,
    quickShortcuts.length > 0 ? `Directives: ${quickShortcuts.join(", ")}` : "",
  ]
    .filter(Boolean)
    .join(" | ");

  const res = await fetch(`${API_BASE}/api/director/plan`, {
    method: "POST",
    headers: getAuthHeaders({ "Content-Type": "application/json" }),
    body: JSON.stringify({
      beats: beatList,
      profile: profile || "historical_docudrama",
      notes: combinedNotes,
      critique: true,
      replan,
    }),
  });

  const data = await res.json().catch(() => ({}));
  if (!res.ok || !data.ok) {
    // The status travels with the error. 409 is not a failure and not something
    // the user did wrong -- start_job refused because a plan for this beat is
    // already running, and it will finish. A caller that only sees the message
    // cannot tell that apart from "planning failed", which is how a refusal
    // came to be reported as a success.
    const err = new Error(
      data.error || `Planning failed with status ${res.status}`
    ) as Error & { status?: number };
    err.status = res.status;
    throw err;
  }

  return data;
}

/**
 * Wait for a background job named by the server to finish.
 *
 * POST /api/director/plan returns {ok, started, job} the instant start_job spawns
 * a thread; a rounds:2 plan -> critic -> re-plan cycle is tens of seconds of
 * Anthropic calls. Callers that refetched immediately got the OLD plan back, or
 * "No coverage plan found" -- so the user re-clicked, start_job returned False,
 * and the UI showed a hard 409 for a job that was running fine and would
 * overwrite their work a minute later.
 */
export async function waitForJob(
  jobKey: string,
  opts: { onLog?: (log: string) => void; timeoutMs?: number; intervalMs?: number } = {}
): Promise<{ ok: boolean; status: string; log: string }> {
  const { onLog, timeoutMs = 15 * 60 * 1000, intervalMs = 2000 } = opts;
  const started = Date.now();
  let last = "";
  for (;;) {
    const res = await fetch(`${API_BASE}/api/assemble/status`, { headers: getAuthHeaders() });
    const data = await res.json().catch(() => ({}));
    const job = data?.jobs?.[jobKey];
    if (job) {
      if (job.log && job.log !== last) {
        last = job.log;
        onLog?.(job.log);
      }
      if (job.status !== "running") {
        return { ok: job.status === "done", status: job.status, log: job.log || "" };
      }
    }
    if (Date.now() - started > timeoutMs) {
      return { ok: false, status: "timeout", log: last };
    }
    await new Promise((r) => setTimeout(r, intervalMs));
  }
}

/** What the survey needs to know about a beat it is offering to plan. */
export interface BeatCoverageState {
  /** The plan's own status: "draft", "locked", "compiled", … */
  status: string;
  /** How many DirectorShots the plan holds. */
  shots: number;
  /** Locked or compiled coverage: finished work the server refuses to overwrite. */
  locked: boolean;
  /**
   * The server's own estimate for this beat, in dollars, or null.
   *
   * `null` when `/api/director/scene`'s summary did not price this beat, and
   * null must render as "not priced" — never as 0, and never as a number
   * borrowed from anywhere else. A cost is the one number on this screen a
   * human will act on; a wrong one is worse than an absent one.
   */
  estimatedCost: number | null;
  /** Critic findings recorded against the plan. */
  warnings: number;
  /** Narration seconds this beat runs, as the server measured it. */
  durationSeconds: number | null;
}

/**
 * Which of these beats already have coverage, and whether it is locked.
 *
 * GET /api/director/survey is pure arithmetic over narration — it never reads a
 * plan, so it cannot tell a planned beat from an unplanned one. That is why the
 * survey went on offering PLAN SCENE for a beat whose coverage was locked, and
 * why clicking it earned a refusal the user had been invited into:
 *
 *   ValueError: every requested beat already has locked coverage (s001).
 *                Pass replan=true to plan over it.
 *
 * The server is right to refuse — locked coverage is work that was reviewed,
 * had its warnings resolved, and was deliberately locked. One read of
 * /api/director/scene covers every beat at once, so the offer can match what
 * the server will actually accept.
 */
export async function fetchBeatCoverageStates(
  beatIds: string[]
): Promise<Record<string, BeatCoverageState>> {
  if (beatIds.length === 0) return {};
  const res = await fetch(
    `${API_BASE}/api/director/scene?beats=${encodeURIComponent(beatIds.join(","))}`,
    { headers: getAuthHeaders() }
  );
  const data = await res.json().catch(() => ({}));
  if (!res.ok || !data.ok) {
    throw new Error(data.error || `Could not read existing coverage (${res.status})`);
  }
  /** Only the parts of a `/api/director/scene` entry this reader needs. */
  interface SceneEntry {
    beat_id?: string;
    beat_duration?: number | null;
    plan?: { status?: string; coverage?: unknown[]; warnings?: unknown[] } | null;
  }
  /** One row of `planner.scene_summary` — the ONLY source of a cost figure. */
  interface SummaryRow {
    beat_id?: string;
    estimated_cost?: number;
  }
  const priced = new Map<string, number>();
  ((data.summary?.beats || []) as SummaryRow[]).forEach((row) => {
    if (row?.beat_id && typeof row.estimated_cost === "number") {
      priced.set(row.beat_id, row.estimated_cost);
    }
  });

  const out: Record<string, BeatCoverageState> = {};
  (data.beats || []).forEach((entry: SceneEntry) => {
    if (!entry?.plan || !entry.beat_id) return;
    const status = String(entry.plan.status || "draft");
    out[entry.beat_id] = {
      status,
      shots: (entry.plan.coverage || []).length,
      // Both statuses mean the same thing to the planner: it will refuse
      // without replan. Deciding that here, once, keeps the client from
      // inventing a second definition of "locked" that can drift from the one
      // the server enforces.
      locked: status === "locked" || status === "compiled",
      // Strictly the server's figure for THIS beat of THIS project. Absent
      // stays absent: no zero, no total divided up, no default.
      estimatedCost: priced.has(entry.beat_id) ? (priced.get(entry.beat_id) as number) : null,
      warnings: (entry.plan.warnings || []).length,
      durationSeconds: typeof entry.beat_duration === "number" ? entry.beat_duration : null,
    };
  });
  return out;
}

/** The job key POST /api/director/plan derives for a beat (`backend/main.py`). */
export const PLAN_JOB_PREFIX = "director_plan:";

/**
 * Which coverage-plan jobs the server has running right now, beat -> job key.
 *
 * The job outlives the browser. `start_job` spawns a server-side thread and the
 * ~90s plan carries on regardless of what the page does — but the state that
 * remembers it is React state, so a remount destroys it: `planningBeats` is
 * wiped, every button re-enables, and the pending `waitForJob` promise is
 * orphaned with its `setState` calls landing on a dead component. The plan then
 * lands with nobody watching for it, which reads exactly like the plan never
 * running.
 *
 * Lifting the state to a parent would only survive a child remount. This
 * survives a page reload and a second tab too, because the server — not the
 * browser — is what actually knows a plan is running.
 */
export async function fetchRunningPlanJobs(): Promise<Record<string, string>> {
  const res = await fetch(`${API_BASE}/api/assemble/status`, { headers: getAuthHeaders() });
  const data = await res.json().catch(() => ({}));
  const jobs = (data && data.jobs) || {};
  const running: Record<string, string> = {};
  Object.keys(jobs).forEach((key) => {
    if (!key.startsWith(PLAN_JOB_PREFIX)) return;
    if (jobs[key]?.status !== "running") return;
    running[key.slice(PLAN_JOB_PREFIX.length)] = key;
  });
  return running;
}

/**
 * A lock or unlock the server refused, carrying what it refused *for*.
 *
 * `POST /api/director/lock_scene` validates every beat before it locks any, and
 * when one fails it answers `400 {"error": "nothing was locked", "problems":
 * [...]}`. The headline is deliberately terse; `problems` is where the route
 * puts the sentence that says what to do — "s001: 6 critic warning(s) awaiting
 * a decision (w_3f2a, w_91bc, …)", "s002: currently compiling", or whatever
 * `director.validate` raised. Thrown as `new Error(data.error)` all of that is
 * dropped and the caller is left with three words that name no beat, no count
 * and no finding.
 *
 * `changed` exists because unlocking is NOT symmetric with locking. There is no
 * bulk unlock route, so a scene unlocks by fanning out one request per beat, and
 * those requests succeed and fail independently: a scene can come back part
 * unlocked. The caller has to be told which beats moved, because the plan on its
 * screen is no longer a description of any of them.
 */
export type LockRefusal = Error & {
  status?: number;
  /** The scene route's per-beat sentences, one per beat that failed. */
  problems?: string[];
  /** The per-beat route's undecided findings, sent with its 400. */
  warnings?: DirectorWarning[];
  /** Beats this call DID change before another refused (unlock fan-out only). */
  changed?: string[];
};

/** The reply's own words, or the status if it had none. Never a summary. */
function lockRefusal(
  status: number,
  data: { error?: unknown; detail?: unknown; problems?: unknown; warnings?: unknown },
  fallback: string
): LockRefusal {
  // `detail` as well as `error`: `beats[] is required` is an HTTPException, so
  // FastAPI serialises it under `detail` and there is no `error` key to read.
  const said = data.error || data.detail;
  const err = new Error(said ? String(said) : fallback) as LockRefusal;
  err.status = status;
  if (Array.isArray(data.problems) && data.problems.length > 0) {
    err.problems = data.problems.map((p) => String(p));
  }
  if (Array.isArray(data.warnings) && data.warnings.length > 0) {
    err.warnings = data.warnings as DirectorWarning[];
  }
  return err;
}

/** A reply body, or `{}` when the response was not JSON at all (502 HTML). */
async function bodyOf(res: Response): Promise<Record<string, unknown>> {
  return (await res.json().catch(() => ({}))) as Record<string, unknown>;
}

/**
 * POST /api/director/lock/{beat_id}?locked=true|false OR POST /api/director/lock_scene
 * Requires X-Studio-Key auth header
 */
export async function setCoverageStatus(
  beatIdOrBeats: string | string[],
  locked: boolean = true
): Promise<{ ok: boolean; status?: string; error?: string }> {
  // lock_scene ONLY locks -- it sets status = "locked" unconditionally and records
  // a `locked` ledger outcome. The array branch used to run for every call and
  // ignore `locked` entirely, so "UNLOCK TO EDIT" locked harder while the UI
  // optimistically showed "draft". The user then edited a scene the server
  // considered locked, and the render path kept skipping those beats.
  if (Array.isArray(beatIdOrBeats) && locked) {
    const res = await fetch(`${API_BASE}/api/director/lock_scene`, {
      method: "POST",
      headers: getAuthHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify({ beats: beatIdOrBeats }),
    });
    const data = await bodyOf(res);
    if (!res.ok || !data.ok) {
      // `problems` travels with it. "nothing was locked" is the route's headline
      // and it is true; the list under it is the only part that names the beat
      // and the findings, and it is what the human needs to act.
      throw lockRefusal(
        res.status,
        data,
        `Locking ${beatIdOrBeats.join(", ")} failed with status ${res.status}.`
      );
    }
    return data as { ok: boolean; status?: string };
  }

  // Unlocking a scene: no bulk route exists, so fan out per beat. Which means
  // these succeed and fail INDEPENDENTLY — unlike lock_scene, which validates
  // every beat before it writes any. A refusal here does not mean nothing
  // changed, so what did change is reported alongside it.
  if (Array.isArray(beatIdOrBeats)) {
    const results = await Promise.all(
      beatIdOrBeats.map(async (b) => {
        const r = await fetch(
          `${API_BASE}/api/director/lock/${encodeURIComponent(b)}?locked=false`,
          { method: "POST", headers: getAuthHeaders() }
        );
        const d = await bodyOf(r);
        // `r.ok` as well as `d.ok`: a 502 from a proxy has no body to read, and
        // reading only the absent `ok` field would have called it a refusal
        // with nothing to say. It is still a refusal — it just has to say the
        // status instead.
        return { beat: b, ok: r.ok && d.ok === true, status: r.status, data: d };
      })
    );
    const bad = results.filter((r) => !r.ok);
    if (bad.length > 0) {
      const sentences = bad.map(
        (r) =>
          `${r.beat}: ${
            r.data.error || r.data.detail || `unlocking failed with status ${r.status}`
          }`
      );
      const err = lockRefusal(
        bad[0].status,
        // One failed beat: its own sentence is the headline, because there is
        // nothing to summarise. Several: the headline says how many, and every
        // one of them is listed below it.
        bad.length === 1 ? bad[0].data : {},
        `${bad.length} of ${results.length} beats refused to unlock.`
      );
      err.problems = sentences;
      err.changed = results.filter((r) => r.ok).map((r) => r.beat);
      throw err;
    }
    return { ok: true, status: "draft" };
  }

  const res = await fetch(
    `${API_BASE}/api/director/lock/${encodeURIComponent(beatIdOrBeats)}?locked=${locked}`,
    {
      method: "POST",
      headers: getAuthHeaders(),
    }
  );

  const data = await bodyOf(res);
  if (!res.ok || !data.ok) {
    // This route names its own count and its own finding ids in `error`, and
    // sends the findings themselves under `warnings`.
    throw lockRefusal(
      res.status,
      data,
      `${locked ? "Locking" : "Unlocking"} beat ${beatIdOrBeats} failed with status ${res.status}.`
    );
  }

  return data as { ok: boolean; status?: string };
}

/**
 * A compile the server refused, carrying what it refused *for*.
 *
 * `compile_director_coverage` answers four distinct 409s and a 404, and each
 * message says something different about what to do next: lock the plan, review
 * a drifted approval, re-approve after a script edit, decide the outstanding
 * critic warnings, or wait for the compile already running. Thrown as a bare
 * Error those become one undifferentiated "compile failed", which throws away
 * the most useful thing the backend produces.
 *
 * The discriminators below are the server's OWN payload fields, not a guess
 * parsed out of the message text — so a reworded message cannot silently
 * re-classify a refusal.
 */
export type CompileRefusal = Error & {
  status?: number;
  /** Present on the draft 409. `true` = it WAS approved and then drifted. */
  approvalDrifted?: boolean;
  /** Present when the beat's script line changed under the plan. */
  stale?: Record<string, unknown>;
  /** Present when a locked plan still carries undecided critic findings. */
  warnings?: DirectorWarning[];
  /** Present when the plan changed after the human was quoted a price. */
  signatureMismatch?: boolean;
  /** Present when the request never said which plan it was approving. */
  signatureMissing?: boolean;
  /** The plan that is there NOW, so the caller can re-quote from it. */
  planSignature?: string;
};

/** Everything POST /api/director/compile/{beat} can answer with, either way. */
type CompileReply = {
  ok?: boolean;
  started?: boolean;
  job?: string;
  beat_id?: string;
  shots?: number;
  /** The refusal, in the route's own words. */
  error?: string;
  /** The 404's words: that one is an HTTPException, so FastAPI uses `detail`. */
  detail?: string;
  approval_drifted?: boolean;
  stale?: Record<string, unknown>;
  warnings?: DirectorWarning[];
  signature_mismatch?: boolean;
  signature_missing?: boolean;
  quoted_signature?: string;
  plan_signature?: string;
};

/**
 * POST /api/director/compile/{beat_id} — render the coverage, buy the paid shots.
 *
 * This is the endpoint a locked plan exists for, and until now nothing in the
 * studio called it: the user could plan a scene, resolve its warnings and lock
 * it, and there was no control anywhere that turned that into assets.
 *
 * Like the planner it answers the instant `start_job` spawns a thread, so `job`
 * is the only honest signal of completion — see `waitForJob`.
 *
 * There is deliberately no `force` parameter. The backend removed one because
 * `force=true` skipped the draft check and could send an unapproved plan into
 * paid generation; the recovery is to lock the plan, not to step over the gate.
 *
 * `planSignature` is what makes the confirmed PRICE bind the plan it was quoted
 * for. Without it the request named only a beat, and the route dispatched
 * whatever `load_plan(beat_id)` returned at the moment it ran — so a plan
 * replaced and re-locked in another tab between the gate opening and the human
 * confirming compiled at the newer price on consent given for the older one.
 * It is REQUIRED, and the route refuses without it. It was optional for one
 * round and optional meant unenforced: `if plan_signature and ...` skipped the
 * comparison for an omitted or empty value, so an unsigned request dispatched
 * whatever plan was on disk. A caller that does not say what it agreed to has
 * not agreed to anything. The parameter is still omitted from the URL when
 * there is nothing to send, because "sent nothing" and "sent an empty string"
 * are the same refusal and a request log should show which one happened.
 */
export async function compileCoverage(
  beatId: string,
  planSignature = ""
): Promise<CompileReply> {
  const query = planSignature
    ? `?plan_signature=${encodeURIComponent(planSignature)}`
    : "";
  const res = await fetch(
    `${API_BASE}/api/director/compile/${encodeURIComponent(beatId)}${query}`,
    { method: "POST", headers: getAuthHeaders() }
  );

  const data: CompileReply = await res.json().catch(() => ({}));
  if (!res.ok || !data.ok) {
    // `detail` is the 404: that one is raised as an HTTPException, so FastAPI
    // serialises it under `detail` and there is no `error` key to read.
    const err = new Error(
      data.error || data.detail || `Compiling ${beatId} failed with status ${res.status}`
    ) as CompileRefusal;
    err.status = res.status;
    if (typeof data.approval_drifted === "boolean") {
      err.approvalDrifted = data.approval_drifted;
    }
    if (data.stale) err.stale = data.stale;
    if (data.warnings) err.warnings = data.warnings;
    if (data.signature_mismatch) err.signatureMismatch = true;
    if (data.signature_missing) err.signatureMissing = true;
    if (data.plan_signature) err.planSignature = data.plan_signature;
    throw err;
  }

  return data;
}

/** What POST /api/director/critique can answer with, either way. */
type CritiqueReply = {
  ok?: boolean;
  /** Absent is NOT the same as empty: see `critiqueCoverage`. */
  warnings?: DirectorWarning[];
  summary?: { shots?: number; paid_shots?: number; estimated_cost?: number };
  error?: string;
  /** `beats[] is required` is an HTTPException, so it arrives under `detail`. */
  detail?: string;
};

/**
 * POST /api/director/critique — re-run the critic over a scene's saved plans.
 *
 * Synchronous, unlike planning and compiling: the reply IS the result, so there
 * is no job to wait for. What there IS to get right is the failure, because the
 * caller's screen does not change on a failure and an unchanged warning list
 * reads exactly like "the critic ran and found nothing new". That is the most
 * dangerous wrong answer this endpoint can produce: the human's next actions are
 * to lock the scene and then spend on it.
 *
 * So three things travel with a failure that previously did not:
 *
 *   - `detail`, because `beats[] is required` is raised as an HTTPException and
 *     FastAPI serialises those under `detail`, leaving no `error` key to read;
 *   - the status, so a caller can tell refusals apart;
 *   - a guarded `res.json()`, because a gateway HTML error page used to throw a
 *     SyntaxError out of the parse and reach the caller as "Unexpected token <".
 *
 * `warnings` is optional in the return type on purpose. A 200 that carries no
 * warning list has re-checked nothing, and a caller that treats the absent case
 * as "clean" would clear the scene on the strength of a reply that never said so.
 *
 * Requires X-Studio-Key auth header.
 */
export async function critiqueCoverage(beats: string[]): Promise<CritiqueReply> {
  const res = await fetch(`${API_BASE}/api/director/critique`, {
    method: "POST",
    headers: getAuthHeaders({ "Content-Type": "application/json" }),
    body: JSON.stringify({ beats }),
  });
  const data: CritiqueReply = await res.json().catch(() => ({}));
  if (!res.ok || !data.ok) {
    const err = new Error(
      data.error ||
        data.detail ||
        `Re-checking ${beats.join(", ")} failed with status ${res.status}`
    ) as Error & { status?: number };
    err.status = res.status;
    throw err;
  }
  return data;
}

/**
 * POST /api/director/shot/{shotId}/action
 * Requires X-Studio-Key auth header
 */
/**
 * POST /api/director/shot/{shot_id} — the only sanctioned way to edit a shot.
 *
 * The server recomputes estimated_cost, re-routes the backend, rebalances the
 * sibling shots so the coverage still sums to the beat, and revalidates. None of
 * that can be done in the browser: coverage that does not sum to its beat cannot
 * compile, and a paid shot at an unproducible length fails only at generation
 * time, after money has been committed.
 */
export async function updateShot(
  shotId: string,
  patch: Record<string, unknown>
): Promise<{
  ok: boolean;
  shot?: DirectorShot;
  notes?: string[];
  problems?: string[];
  coverage_total?: number;
  beat_duration?: number;
  error?: string;
}> {
  const res = await fetch(`${API_BASE}/api/director/shot/${encodeURIComponent(shotId)}`, {
    method: "POST",
    headers: getAuthHeaders({ "Content-Type": "application/json" }),
    body: JSON.stringify(patch),
  });
  const data = await res.json();
  if (!res.ok || !data.ok) {
    throw new Error(data.error || data.detail || `Editing ${shotId} failed (${res.status})`);
  }
  return data;
}

/** Which quick actions the server can actually serve today. */
export const SHOT_ACTIONS_SUPPORTED = ["alternate_angle"] as const;

/**
 * Quick actions. Only those expressible through the shot endpoint are real.
 *
 * This used to POST /api/director/shot/{id}/action, which is not a registered
 * route -- FastAPI path params do not match across "/", so every Regenerate Take,
 * Alternate Angle, Replace Shot and Delete Shot click 404'd and threw inside an
 * un-caught handler. No banner, no error, the buttons simply appeared inert.
 * Rather than keep a call to a route that does not exist, the one action the
 * server CAN serve now goes through the real endpoint and the rest say so.
 */
const ANGLE_CYCLE = ["front", "three_quarter", "low", "high", "profile"];

export async function performShotAction(
  shotId: string,
  action: string,
  shot?: DirectorShot
): Promise<{ ok: boolean; shot?: DirectorShot; supported: boolean; error?: string }> {
  if (action === "alternate_angle") {
    const current = (shot?.angle || "").trim();
    const i = ANGLE_CYCLE.indexOf(current);
    const next = ANGLE_CYCLE[(i + 1) % ANGLE_CYCLE.length];
    const data = await updateShot(shotId, { angle: next });
    return { ok: true, shot: data.shot, supported: true };
  }
  return {
    ok: false,
    supported: false,
    error: `"${action}" has no server route yet — it would need a new endpoint. ` +
           `Edit the shot's fields instead.`,
  };
}

/**
 * GET /api/director/survey
 * Answers which beats are worth covering before planning
 */
export async function fetchCoverageSurvey(): Promise<CoverageSurvey> {
  const res = await fetch(`${API_BASE}/api/director/survey`, { headers: getAuthHeaders() });
  if (!res.ok) {
    throw new Error(`Survey endpoint returned ${res.status} ${res.statusText}`);
  }
  const data = await res.json();
  if (!data.ok) {
    throw new Error(data.error || "Failed to fetch coverage survey");
  }
  return data;
}

// Real beat ID mock constant for unit testing / UI preview if needed
export const MOCK_SCENES: SceneSummary[] = [
  {
    scene_id: "s004",
    title: "s004 — The Mountain Takes Its Toll",
    duration: 72,
    beats_count: 3,
    shots_count: 11,
    estimated_cost: 3.82,
    status: "draft",
    warnings_count: 2,
  },
];

/**
 * POST /api/director/warning/{beatId}/{warningId}
 *
 * Records a durable decision about one critic finding. The studio previously
 * had no way to do this: "resolve" filtered the warning out of React state and
 * nothing else, so the screen showed a clean review while the persisted plan
 * still carried the finding, and a refresh brought it back.
 *
 * `decision` is "resolved" (the plan was changed to answer it) or "accepted"
 * (understood and deliberately kept). Passing "" clears the decision again.
 * Returns the server's warning list, which callers should use verbatim rather
 * than editing their local copy.
 */
export async function decideDirectorWarning(
  beatId: string,
  warningId: string,
  decision: "resolved" | "accepted" | "",
  note = ""
): Promise<{
  ok: boolean;
  warnings: DirectorWarning[];
  warning_dispositions: Record<string, WarningDisposition>;
  unresolved: number;
  error?: string;
}> {
  const res = await fetch(
    `${API_BASE}/api/director/warning/${encodeURIComponent(beatId)}/${encodeURIComponent(warningId)}`,
    {
      method: "POST",
      headers: getAuthHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify({ decision, note }),
    }
  );
  const data = await res.json();
  if (!res.ok || !data.ok) {
    throw new Error(data.error || `Could not record that decision (${res.status})`);
  }
  return data;
}
