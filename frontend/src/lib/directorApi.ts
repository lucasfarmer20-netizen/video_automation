import {
  DirectorCoveragePlan,
  DirectorShot,
  DirectorWarning,
  SceneSummary,
  CreativePreferences,
  DirectorProfilesResponse,
} from "../types/director";

const API_BASE =
  typeof window !== "undefined"
    ? window.location.hostname === "localhost"
      ? "http://localhost:5000"
      : ""
    : "";

/**
  * GET /api/director/profiles
  * Authoritative profiles, vocabulary, and video_capabilities
  */
export async function fetchDirectorProfiles(): Promise<DirectorProfilesResponse> {
  const res = await fetch(`${API_BASE}/api/director/profiles`);
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
  * Read model for a scene or set of beats
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

  const res = await fetch(url);
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
  * Request creative redirection or scene coverage re-planning
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
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      beats: beatList,
      profile: profile || "historical_docudrama",
      notes: combinedNotes,
      critique: true,
    }),
  });

  const data = await res.json();
  if (!res.ok || !data.ok) {
    throw new Error(data.error || `Planning failed with status ${res.status}`);
  }

  return data;
}

/**
  * POST /api/director/lock/{beat_id}?locked=true|false OR POST /api/director/lock_scene
  */
export async function setCoverageStatus(
  beatIdOrBeats: string | string[],
  locked: boolean = true
): Promise<{ ok: boolean; status?: string; error?: string }> {
  if (Array.isArray(beatIdOrBeats)) {
    const res = await fetch(`${API_BASE}/api/director/lock_scene`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ beats: beatIdOrBeats }),
    });
    const data = await res.json();
    if (!res.ok || !data.ok) {
      throw new Error(data.error || `Locking scene failed with status ${res.status}`);
    }
    return data;
  }

  const res = await fetch(
    `${API_BASE}/api/director/lock/${encodeURIComponent(beatIdOrBeats)}?locked=${locked}`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
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
  */
export async function critiqueCoverage(beats: string[]): Promise<{ ok: boolean; warnings: DirectorWarning[] }> {
  const res = await fetch(`${API_BASE}/api/director/critique`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
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
  */
export async function performShotAction(
  shotId: string,
  action: string,
  payload?: any
): Promise<{ ok: boolean; updatedShot?: DirectorShot; error?: string }> {
  const res = await fetch(`${API_BASE}/api/director/shot/${encodeURIComponent(shotId)}/action`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ action, payload }),
  });

  const data = await res.json();
  if (!res.ok || !data.ok) {
    throw new Error(data.error || `Shot action ${action} failed with status ${res.status}`);
  }

  return data;
}

// Optional exported mock constants for unit testing if required
export const MOCK_SCENES: SceneSummary[] = [
  {
    scene_id: "s004",
    title: "04 — The Mountain Takes Its Toll",
    duration: 72,
    beats_count: 3,
    shots_count: 11,
    estimated_cost: 3.82,
    status: "draft",
    warnings_count: 2,
  },
];
