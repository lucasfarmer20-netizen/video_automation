import {
  DirectorCoveragePlan,
  DirectorShot,
  DirectorWarning,
  SceneSummary,
  CreativePreferences,
} from "../types/director";

const API_BASE =
  typeof window !== "undefined"
    ? window.location.hostname === "localhost"
      ? "http://localhost:5000"
      : ""
    : "";

// Mock initial scenes list for Film Overview
export const MOCK_SCENES: SceneSummary[] = [
  {
    scene_id: "scene_01",
    title: "01 — Arrival",
    duration: 48,
    beats_count: 2,
    shots_count: 6,
    estimated_cost: 2.1,
    status: "compiled",
    warnings_count: 0,
  },
  {
    scene_id: "scene_02",
    title: "02 — The Impossible Pass",
    duration: 66,
    beats_count: 3,
    shots_count: 9,
    estimated_cost: 3.15,
    status: "locked",
    warnings_count: 0,
  },
  {
    scene_id: "scene_03",
    title: "03 — Heney's Plan",
    duration: 54,
    beats_count: 2,
    shots_count: 7,
    estimated_cost: 1.8,
    status: "generating",
    warnings_count: 0,
  },
  {
    scene_id: "scene_04",
    title: "04 — The Mountain Takes Its Toll",
    duration: 72,
    beats_count: 3,
    shots_count: 11,
    estimated_cost: 3.82,
    status: "draft",
    warnings_count: 2,
  },
  {
    scene_id: "scene_05",
    title: "05 — Construction",
    duration: 68,
    beats_count: 3,
    shots_count: 8,
    estimated_cost: 0.0,
    status: "uncovered",
    warnings_count: 0,
  },
  {
    scene_id: "scene_06",
    title: "06 — Disaster",
    duration: 59,
    beats_count: 2,
    shots_count: 8,
    estimated_cost: 2.4,
    status: "draft",
    warnings_count: 3,
  },
];

// Mock coverage plan for Scene 4 matching the UX brief requirements exactly
export const MOCK_SCENE_4_COVERAGE: DirectorCoveragePlan = {
  plan_id: "plan_s004_v2",
  scene_id: "scene_04",
  scene_title: "Scene 4 — The Mountain Takes Its Toll",
  scene_beats: ["s004", "s005"],
  status: "draft",
  total_duration: 72.0,
  beat_duration: 72.0,
  coverage_total: 72.0,
  profile: "historical_docudrama",
  visual_strategy:
    "Hold the mountain pass in wide, then let the failure play out in inserts — the timber cables, ice spikes, casualty parchment, and exhausted worker reactions.",
  blocking: {
    environment: "Snowbound railway construction ledge, heavy canvas tents, dark mountain ravine fog",
    characters: ["Heney", "Company Representative", "Laborers"],
    props: ["Casualty report parchment", "Iron spikes", "Pickaxes", "Timber cables"],
  },
  estimated_cost: 3.82,
  warnings: [
    {
      id: "warn_01",
      beat_id: "s004",
      shot_id: "s004.05",
      kind: "identity_risk",
      detail:
        "Identity-critical CU uses Three-quarter OTS instead of frontal close-up due to reference confidence ceiling.",
      suggestion: "Review identity variations or switch reference image",
      severity: "warning",
    },
    {
      id: "warn_02",
      beat_id: "s004",
      shot_id: "s004.07",
      kind: "timing_mismatch",
      detail: "Coverage duration (22.4s) extends 0.8s past narration audio timing.",
      suggestion: "Trim s004.07 duration by 0.8s",
      severity: "warning",
    },
  ],
  coverage: [
    {
      id: "shot_04_01",
      beat_id: "s004",
      shot_number: "04.01",
      purpose: "establishing",
      subject: "EXTERIOR_TENT",
      shot_size: "ws",
      angle: "eye_level",
      composition: "Wide environmental framing",
      camera: { move: "pan_right", duration: 3.0, speed: 0.8, amount: 0.12 },
      identity_critical: false,
      motion_type: "parallax",
      backend: "nano2",
      prompt:
        "Historical woodblock engraving of a snowbound railway construction encampment, canvas tents shuddering in bitter mountain wind, deep chiaroscuro shadow.",
      motion_prompt: "Slow horizontal pan right revealing desolate tents in snowstorm",
      reason: "Establish harsh encampment environment before entering narration.",
      draft_variations: [],
      estimated_cost: 0.0,
      status: "ready",
    },
    {
      id: "shot_04_02",
      beat_id: "s004",
      shot_number: "04.02",
      purpose: "detail",
      subject: "DRIPPING_WATER",
      shot_size: "ecu",
      angle: "overhead",
      composition: "Extreme close-up macro detail",
      camera: { move: "static", duration: 2.0, speed: 0, amount: 0 },
      identity_critical: false,
      motion_type: "static",
      backend: "nano2",
      prompt:
        "Macro detail of ice melt dripping off rusted iron spikes on wooden railway ties, dark folklore ink texture, harsh highlights.",
      motion_prompt: "Static shot with subtle candle flicker fx",
      reason: "Atmospheric beat signaling melting ice and passage of time.",
      draft_variations: [],
      estimated_cost: 0.0,
      status: "ready",
    },
    {
      id: "shot_04_03",
      beat_id: "s004",
      shot_number: "04.03",
      purpose: "master",
      subject: "MASTER_PASS",
      shot_size: "ws",
      angle: "three_quarter",
      composition: "High angle master cut",
      camera: { move: "push_in", duration: 4.0, speed: 1.0, amount: 0.15 },
      identity_critical: false,
      motion_type: "ai_video",
      backend: "seedance_2_0",
      prompt:
        "High three-quarter panoramic view of workers clearing a rocky mountain ledge for tracks, heavy timber framing, moody fog rolling into ravine.",
      motion_prompt: "Camera pushes forward into valley as workers haul timber cables",
      reason: "The wide is the only place the machine reads as bigger than the men.",
      constrained_by: ["duration_quantized"],
      draft_variations: [],
      estimated_cost: 1.25,
      status: "ready",
    },
    {
      id: "shot_04_04",
      beat_id: "s004",
      shot_number: "04.04",
      purpose: "insert",
      subject: "REPORT_INSERT",
      shot_size: "cu",
      angle: "overhead",
      composition: "Overhead document insert",
      camera: { move: "tilt_down", duration: 2.5, speed: 0.9, amount: 0.1 },
      identity_critical: false,
      motion_type: "parallax",
      backend: "nano2",
      prompt:
        "1890s hand-written engineering report on yellowed parchment, steel-nib ink listing casualties and timber tons, flickering lantern light.",
      motion_prompt: "Gentle tilt down paper surface",
      reason: "Close-up on casualty report document to ground dialogue in facts.",
      draft_variations: [],
      estimated_cost: 0.0,
      status: "ready",
    },
    {
      id: "shot_04_05",
      beat_id: "s004",
      shot_number: "04.05",
      purpose: "reaction",
      subject: "HENEY_01",
      shot_size: "cu",
      angle: "ots",
      composition: "Tight character reaction",
      camera: { move: "push_in", duration: 3.2, speed: 1.2, amount: 0.18 },
      identity_critical: true,
      constrained_by: ["identity_reliability"],
      motion_type: "parallax",
      backend: "nano2",
      prompt:
        "Close-up of Michael Heney, rugged 1890s railroad contractor, weathered face, dark intense eyes under brimmed hat, stern grim expression.",
      motion_prompt: "Subtle slow push in on face as light shifts",
      reason: "Delay Heney's reaction until the casualty report has landed.",
      draft_variations: [
        "/media/assets/s004/var_01.png",
        "/media/assets/s004/var_02.png",
        "/media/assets/s004/var_03.png",
        "/media/assets/s004/var_04.png",
      ],
      chosen_variation: 1,
      estimated_cost: 0.0,
      status: "ready",
    },
    {
      id: "shot_04_06",
      beat_id: "s004",
      shot_number: "04.06",
      purpose: "cutaway",
      subject: "COMPANY_REP",
      shot_size: "mcu",
      angle: "front",
      composition: "Medium close-up dialogue",
      camera: { move: "pan_left", duration: 3.7, speed: 1.0, amount: 0.15 },
      identity_critical: false,
      motion_type: "ai_video",
      backend: "seedance_2_0",
      prompt:
        "Formal British railway financier in heavy wool coat standing inside tent, pointing at map on table, stern authoritative posture.",
      motion_prompt: "Financier gestures toward map on table while speaking",
      reason: "Show representative delivering corporate instruction.",
      draft_variations: [],
      estimated_cost: 1.4,
      status: "ready",
    },
    {
      id: "shot_04_07",
      beat_id: "s004",
      shot_number: "04.07",
      purpose: "cutaway",
      subject: "WORKERS_OUTSIDE",
      shot_size: "mw",
      angle: "low",
      composition: "Group labor medium shot",
      camera: { move: "push_in", duration: 4.0, speed: 0.7, amount: 0.12 },
      identity_critical: false,
      motion_type: "parallax",
      backend: "nano2",
      prompt:
        "Low-angle view of exhausted laborers in thick wool coats swinging pickaxes into frozen granite, silhouetted against twilight sky.",
      motion_prompt: "Parallax depth motion drifting across workers",
      reason: "Contextualize dialogue with exhausted workers clearing track.",
      draft_variations: [],
      estimated_cost: 0.0,
      status: "ready",
    },
  ],
};

/** API Client functions with fallback to local mock data */
export async function fetchScenes(): Promise<SceneSummary[]> {
  try {
    const res = await fetch(`${API_BASE}/api/director/scenes`);
    const data = await res.json();
    if (data.ok) return data.scenes;
  } catch (e) {
    console.log("Using mock director scenes list");
  }
  return MOCK_SCENES;
}

export async function fetchCoveragePlan(sceneId: string): Promise<DirectorCoveragePlan> {
  try {
    const res = await fetch(`${API_BASE}/api/director/coverage/${sceneId}`);
    const data = await res.json();
    if (data.ok) return data.plan;
  } catch (e) {
    console.log("Using mock director coverage for scene", sceneId);
  }
  return { ...MOCK_SCENE_4_COVERAGE, scene_id: sceneId };
}

export async function redirectSceneCoverage(
  sceneId: string,
  commandText: string,
  quickShortcuts: string[],
  preferences: CreativePreferences
): Promise<{ ok: boolean; plan: DirectorCoveragePlan; message?: string }> {
  try {
    const res = await fetch(`${API_BASE}/api/director/redirect`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        scene_id: sceneId,
        command: commandText,
        shortcuts: quickShortcuts,
        preferences,
      }),
    });
    const data = await res.json();
    if (data.ok) return data;
  } catch (e) {
    console.log("Mock redirect response");
  }

  // Simulate local mock redirect revision
  const newCoverage = MOCK_SCENE_4_COVERAGE.coverage.map((s) => {
    if (quickShortcuts.includes("Fewer cuts") && s.camera.duration < 3.5) {
      return { ...s, camera: { ...s.camera, duration: s.camera.duration + 1.0 } };
    }
    if (quickShortcuts.includes("Reduce generation cost") && s.motion_type === "ai_video") {
      return { ...s, motion_type: "parallax" as const, estimated_cost: 0.0 };
    }
    return s;
  });

  return {
    ok: true,
    plan: {
      ...MOCK_SCENE_4_COVERAGE,
      coverage: newCoverage,
      estimated_cost: newCoverage.reduce((acc, curr) => acc + curr.estimated_cost, 0),
    },
    message: "Coverage plan updated according to direction.",
  };
}

export async function setCoverageStatus(
  sceneId: string,
  status: "draft" | "locked" | "generating" | "compiled"
): Promise<{ ok: boolean }> {
  try {
    const res = await fetch(`${API_BASE}/api/director/lock`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ scene_id: sceneId, status }),
    });
    const data = await res.json();
    if (data.ok) return data;
  } catch (e) {
    console.log("Mock lock coverage status set to", status);
  }
  return { ok: true };
}

export async function performShotAction(
  shotId: string,
  action: string,
  payload?: any
): Promise<{ ok: boolean; updatedShot?: DirectorShot }> {
  try {
    const res = await fetch(`${API_BASE}/api/director/shot/${shotId}/action`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ action, payload }),
    });
    const data = await res.json();
    if (data.ok) return data;
  } catch (e) {
    console.log("Mock shot action performed:", action, shotId);
  }
  return { ok: true };
}

export async function fetchDirectorProfiles() {
  try {
    const res = await fetch(`${API_BASE}/api/director/profiles`);
    const data = await res.json();
    if (data.ok) return data;
  } catch (e) {
    console.log("Using mock director profiles fallback");
  }
  return {
    ok: true,
    profiles: {
      historical_docudrama: {
        label: "Historical docudrama (Calluses)",
        shot_seconds: [2.5, 5.5] as [number, number],
        camera_motion: "restrained",
        environmental_coverage: "high",
        cutaway_density: "high",
        face_exposure: "moderate",
        max_ai_video_per_scene: 2,
        note: "Observational. Hands, tools, environment and process over faces.",
      },
    },
    default_profile: "historical_docudrama",
    vocabulary: {
      purpose: ["establishing", "master", "reaction", "insert", "cutaway", "detail", "transition"],
      shot_size: ["ews", "ws", "mw", "m", "mcu", "cu", "ecu"],
      angle: ["front", "profile", "three_quarter", "rear_three_quarter", "ots", "high", "low", "overhead"],
      camera_move: ["static", "push_in", "pull_out", "pan_left", "pan_right", "tilt_up", "tilt_down"],
      motion_type: ["static", "parallax", "ai_video"],
    },
    video_capabilities: [
      {
        key: "seedance_2_0",
        label: "Seedance 2.0 (image-to-video)",
        allowed_durations: [3.0, 5.0, 10.0],
        duration_values: ["3s", "5s", "10s"],
        duration_wire_type: "string" as const,
        duration_default: "5s",
        min_seconds: 3.0,
        max_seconds: 10.0,
        cost_per_second: 0.25,
        verified: true,
        needs_start_image: true,
      },
      {
        key: "veo_3_1",
        label: "Google Veo 3.1 (image-to-video)",
        allowed_durations: [4.0, 6.0, 8.0],
        duration_values: ["4s", "6s", "8s"],
        duration_wire_type: "string" as const,
        duration_default: "8s",
        min_seconds: 4.0,
        max_seconds: 8.0,
        cost_per_second: 0.40,
        verified: true,
        needs_start_image: true,
      },
    ],
  };
}
