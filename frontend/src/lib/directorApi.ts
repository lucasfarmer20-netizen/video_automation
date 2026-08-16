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
        estimated_cost: data.summary?.estimated_cost || plan.estimated_cost || 0,
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
  profile?: string
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
 * POST /api/director/critique
 * Requires X-Studio-Key auth header
 */
export async function critiqueCoverage(beats: string[]): Promise<{ ok: boolean; warnings: DirectorWarning[] }> {
  const res = await fetch(`${API_BASE}/api/director/critique`, {
    method: "POST",
    headers: getAuthHeaders({ "Content-Type": "application/json" }),
    body: JSON.stringify({ beats }),
  });
  const data = await res.json();
  if (!res.ok || !data.ok) {
    throw new Error(data.error || `Critique endpoint failed with status ${res.status}`);
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
