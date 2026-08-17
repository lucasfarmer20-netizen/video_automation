import {
  DirectorCoveragePlan,
  DirectorShot,
  DirectorWarning,
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
 * GET /api/director/scene?beats=s004,s005,s006
 * Read model for a scene or set of beats (unauthenticated reads)
 */
export async function fetchCoveragePlan(
  beatIds: string | string[],
  tierFilter?: "needs_review"
): Promise<DirectorCoveragePlan> {
  const beatsParam = Array.isArray(beatIds) ? beatIds.join(",") : beatIds;
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

  // If backend returns beat array shape
  if (data.beats && data.beats.length > 0) {
    const firstBeatWithPlan = data.beats.find((b: any) => b.plan !== null) || data.beats[0];
    if (firstBeatWithPlan && firstBeatWithPlan.plan) {
      const plan = firstBeatWithPlan.plan;
      return {
        plan_id: plan.plan_id || `plan_${beatsParam}`,
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
    const data = await res.json();
    if (!res.ok || !data.ok) {
      throw new Error(data.error || `Locking scene failed with status ${res.status}`);
    }
    return data;
  }

  // Unlocking a scene: no bulk route exists, so fan out per beat.
  if (Array.isArray(beatIdOrBeats)) {
    const results = await Promise.all(
      beatIdOrBeats.map(async (b) => {
        const r = await fetch(
          `${API_BASE}/api/director/lock/${encodeURIComponent(b)}?locked=false`,
          { method: "POST", headers: getAuthHeaders() }
        );
        return r.json();
      })
    );
    const bad = results.find((r) => !r.ok);
    if (bad) throw new Error(bad.error || "Unlocking the scene failed");
    return { ok: true, status: "draft" };
  }

  const res = await fetch(
    `${API_BASE}/api/director/lock/${encodeURIComponent(beatIdOrBeats)}?locked=${locked}`,
    {
      method: "POST",
      headers: getAuthHeaders(),
    }
  );

  const data = await res.json();
  if (!res.ok || !data.ok) {
    throw new Error(data.error || `Locking beat ${beatIdOrBeats} failed with status ${res.status}`);
  }

  return data;
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
 * Sending it is not a courtesy: the route cannot honour a quote it was never
 * told about, and refetching before posting would only narrow the window.
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
