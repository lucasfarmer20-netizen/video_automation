export interface DirectorCamera {
  move: string;
  duration: number; // Duration lives in camera only
  speed: number;
  amount: number;
  duration_locked?: boolean;
}

export interface DirectorShot {
  id: string; // "<beat>.<2-digit ordinal>" e.g. "s004.03"
  beat_id: string;
  shot_number?: string; // e.g. "04.05" or "s004.05"

  // Backend-computed Tier (1 = standard $0.15 still, 2 = check/limit changed, 3 = creative/face anchor)
  tier: 1 | 2 | 3;
  tier_reason?: string;

  // Editorial intent
  purpose: string; // "establishing" | "master" | "reaction" | "insert" | "cutaway" | "detail" | "transition"
  subject: string;
  shot_size: string; // "ews" | "ws" | "mw" | "m" | "mcu" | "cu" | "ecu"
  angle: string; // "front" | "profile" | "three_quarter" | "rear_three_quarter" | "ots" | "high" | "low" | "overhead"
  composition?: string;

  // Timing & motion (duration in camera)
  camera: DirectorCamera;

  // Generation intent
  character_motion?: boolean;
  face_visibility?: "none" | "low" | "moderate" | "high";
  motion_complexity?: "none" | "low" | "medium" | "high";
  gestural?: boolean;
  identity_critical: boolean; // Character face anchor (4 takes)
  reference_dependencies?: string[];

  // Resolved tier & model
  motion_type: "static" | "parallax" | "ai_video";
  backend: string;

  // Prompts & Rationale
  prompt: string;
  motion_prompt: string;
  reason?: string; // Creative rationale explaining why the shot exists

  // Provenance & Constraints
  source?: "generated" | "library";
  source_ref?: string;
  library_scope?: string;
  constrained_by?: string[] | string; // e.g. ["duration_quantized"], ["downgraded_no_legal_model"]

  // Variations & state
  draft_variations: string[];
  chosen_variation?: number | null;
  clip?: string | null;
  estimated_cost: number;
  error?: string;
  status?: "draft" | "generating" | "ready" | "error";
  thumbnail_url?: string | null;
}

export interface WarningDisposition {
  decision: "resolved" | "accepted";
  note?: string;
  by?: string;
}

export interface DirectorWarning {
  /** Server-derived, content-based identity. The key for a disposition. */
  id?: string;
  /** Any id the warning arrived carrying. Kept for tracing only — never the
   *  disposition key, or a changed finding could inherit an old decision. */
  source_id?: string;
  beat_id?: string;
  shot_id?: string;
  kind:
    | "missing_establishing"
    | "repeated_framing"
    | "insufficient_cutaway"
    | "no_reaction_coverage"
    | "impractical_duration"
    | "identity_risk"
    | "unnecessarily_expensive"
    | "timing_mismatch"
    | "missing_reference"
    | "continuity"
    | "other";
  detail: string;
  suggestion?: string;
  suggested_action?: string;
  type?: string;
  message?: string; // Fallback for backwards compatibility
  severity?: "warning" | "error";
  stale?: boolean; // Set true when shot is edited prior to re-critique
}

export interface DirectorTriageTier {
  shots: number;
  cost: number;
  ids: string[];
}

export interface DirectorTriage {
  tiers: {
    "1": DirectorTriageTier;
    "2": DirectorTriageTier;
    "3": DirectorTriageTier;
  };
  needs_review: string[];
}

export interface DirectorCoveragePlan {
  plan_id: string;
  scene_id: string;
  scene_title: string;
  scene_beats: string[];
  status: "draft" | "locked" | "compiling" | "compiled" | "orphaned";
  total_duration: number;
  beat_duration?: number; // SNAPSHOT at plan time
  live_beat_duration?: number; // Authoritative live beat duration
  coverage_total?: number; // Must equal beat_duration to compile
  profile: string; // e.g. "historical_docudrama"
  visual_strategy?: string;
  blocking?: {
    environment?: string;
    characters?: string[];
    props?: string[];
  } | string;
  triage?: DirectorTriage;
  created_by?: "planner" | "manual";
  version?: number;
  coverage: DirectorShot[];
  warnings: DirectorWarning[];
  /** Durable human decision per warning id. A warning with no entry
   *  here is unresolved and blocks locking (contract 5.4). */
  warning_dispositions?: Record<string, WarningDisposition>;
  estimated_cost: number;
  /** Shots that will be bought from a paid video model when this compiles.
   *  Server-counted (`summary.paid_shots`); the compile gate quotes it. */
  paid_shots?: number;
  /** Server-computed identity of the approved plan (`director.plan_signature`).
   *  Sent back with a compile so the price the human confirmed binds the plan
   *  it was quoted for — see `compileCoverage`. Empty on an unapproved plan. */
  approved_signature?: string;
}

export interface SurveyBeat {
  beat_id: string;
  seconds: number;
  motion_type: string;
  frozen_if_left: number;
  recommend: 1 | 2 | 3;
  reason: string;
  narration?: string;
}

export interface CoverageSurvey {
  episode_seconds: number;
  frozen_if_nothing_covered: number;
  frozen_pct: number;
  recommended: string[];
  scenes: string[][];
  beats: SurveyBeat[];
}

export interface VideoCapability {
  key: string;
  label: string;
  allowed_durations: number[] | null;
  duration_values?: string[];
  duration_wire_type?: "string" | "integer";
  duration_default?: string;
  min_seconds: number;
  max_seconds: number;
  cost_per_second: number;
  verified?: boolean;
  needs_start_image?: boolean;
  supports_reference_image?: boolean;
  supports_character_reference?: boolean;
  supports_extend?: boolean;
}

export interface DirectorProfilesResponse {
  ok: boolean;
  profiles: Record<
    string,
    {
      label: string;
      shot_seconds: [number, number];
      camera_motion: string;
      environmental_coverage: string;
      cutaway_density: string;
      face_exposure: string;
      max_ai_video_per_scene: number;
      note: string;
    }
  >;
  default_profile: string;
  vocabulary: {
    purpose: string[];
    shot_size: string[];
    angle: string[];
    camera_move: string[];
    motion_type: string[];
  };
  video_capabilities: VideoCapability[];
}

export interface SceneSummary {
  scene_id: string;
  title: string;
  duration: number;
  beats_count: number;
  shots_count: number;
  estimated_cost: number;
  status: "draft" | "locked" | "generating" | "compiled" | "uncovered";
  warnings_count: number;
}

export interface CreativePreferences {
  documentaryVsCinematic: number;
  restrainedVsDramatic: number;
  economicalVsQuality: number;
}
