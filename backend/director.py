"""Director coverage: many cinematic shots inside one narrative beat.

A ``Shot`` in this pipeline is a *narrative beat* — one narration segment, one
image, one clip, running 20-40 seconds. That is right for illustrated
documentary and wrong for cinema, where the same 27 seconds might be seven
shots. This module lets a beat be assembled from several *Director Shots*
without changing anything the rest of the pipeline depends on.

The seam is deliberate and narrow. Every beat already renders to exactly one
file, ``render/<scene_id>.mp4``, and ``timeline.build_preview`` concatenates
those in order while mixing audio against ``camera.duration``. Nothing
downstream inspects how that clip was made. So coverage renders sub-clips,
concatenates them to exactly the beat's duration, and writes that same file —
and narration, SFX, the mix, fades, the preview, FCPXML, the rough-cut plan and
the export bundle all keep working untouched.

Two consequences of that choice are load-bearing:

* **Nothing here writes ``storyboard_manifest.json``.** Coverage lives in
  ``director/<beat_id>.json``, one small independently-written file per beat.
  Putting a list of mutable objects on ``Shot`` would reintroduce the
  lost-update race that once flattened 25 beats to 6.0s, at a depth
  ``save_shot_assets`` cannot reach: it merges whole *fields*, and two Director
  Shots generating concurrently would each write the whole list back.
* **Sub-clips are normalized explicitly.** ``build_preview`` concatenates with
  ``-c copy``, which demands identical codec, resolution, pixel format, frame
  rate and timebase. That holds today only by accident — every beat clip either
  comes from ``motion.render_shot`` (identical parameters by construction) or is
  a paid clip that ``_pad_clip_to_beat`` re-encoded on its way to beat length.
  Director Shots are 2-5 seconds, so a generated clip can match its shot exactly,
  skip padding, and reach the concat un-normalized. See ``normalize_clip``.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import threading
from dataclasses import asdict, dataclass, field
from pathlib import Path

from . import atomic, config, generation, ledger
from .ffmpeg_bin import ffmpeg_bin, ffprobe_bin
from .manifest import Camera, MotionType, Shot, Storyboard, approval_is_explicit

# Canonical stream parameters. These mirror `motion.render_shot`'s writer
# exactly; if that changes, this must change with it or `-c copy` concat breaks.
CANON_FPS = 24
CANON_HEIGHT = 720
CANON_WIDTH = 1280          # 16:9. Enforced exactly — see normalize_clip.
CANON_CRF = "20"
CANON_PIXFMT = "yuv420p"

# How closely coverage must add up to its beat. Tighter than a frame at 24fps
# (0.042s), because the error compounds across 25 beats and the audio mix is
# laid against the manifest's durations, not the video's.
DURATION_TOLERANCE = 0.05

# How much a gestural clip may be trimmed before the movement is at risk.
#
# The guard exists to stop a sledge blow being cut at 80% of its swing. It fired
# on a 0.04s overrun -- ONE FRAME at 24fps -- because a model asked for 5s
# returned 5.04s, which they routinely do. A quarter second off the tail of a
# five-second shot cannot break a gesture; a 1.7s trim can. The distinction is
# the size of the cut, not whether the shot is gestural at all.
GESTURAL_TRIM_TOLERANCE = 0.25

PLAN_VERSION = 1

# Only one compile renders at a time, process-wide.
#
# Per-beat job keys stop compiles being silently dropped, but they also let two
# run at once — and that killed both. Each parallax shot pipes raw 1280x720 RGB
# into an ffmpeg child while holding depth maps and layer arrays in memory, and
# the deployment is a single Cloud Run instance with max-instances=1. Two of them
# together exhausted it and ffmpeg died mid-write:
#
#   !! s011.03 FAILED: [Errno 32] Broken pipe
#
# File isolation was never the issue — each compile touches only its own plan,
# sub-clips and beat clip. The contention is CPU and memory, which no amount of
# path separation helps. Queueing costs a few minutes of wall clock and is the
# difference between both beats finishing and both failing halfway.
_COMPILE_LOCK = threading.Lock()

# How many times one shot's LOCAL render may be attempted inside a compile.
#
# Two compiles in twenty-three renders died the same way:
#
#   !! s003.06 FAILED: [Errno 32] Broken pipe      (shot 6 of 7)
#   !! s005.05 FAILED: [Errno 32] Broken pipe      (shot 5 of 6)
#
# Both free-tier `parallax`, both the penultimate shot of their beat, both with
# an EMPTY `FFMPEG STDERR OUTPUT` and an EPIPE on the write into ffmpeg's stdin.
# Empty stderr plus an immediate broken pipe is a child that was *killed*, not
# one that rejected its input -- ffmpeg never lived long enough to complain. The
# container survived both (the compile carried on to the next shot), so this is
# not the instance being terminated.
#
# The cause is NOT established. No OOM or termination events appear in the Cloud
# Run logs, and container memory sat around 26% mean of 4Gi with no reliable
# peak. A kernel OOM-killer inside the container picking ffmpeg as the
# highest-RSS process fits the evidence exactly as well as a transient write
# failure against the gcsfuse mount does, and neither is assumed here. What IS
# established is that both were transient: the human re-ran the compile by hand
# and the same shot rendered immediately, with no code change.
#
# So a local render is attempted twice before six finished shots are abandoned
# for the seventh. This is resilience, not a diagnosis, and it must not grow
# into one. See `local_only` in `_compile_locked` for the boundary that keeps it
# away from anything that can be billed.
LOCAL_RENDER_ATTEMPTS = 2


@dataclass
class DirectorShot:
    """One piece of coverage inside a beat.

    ``camera`` carries the duration, as it does for beats — a second duration
    field at this level is exactly how the timeline ended up with two time bases
    and a playhead pointing at the wrong shot.

    The generation-intent fields (``character_motion`` ... ``reference_dependencies``)
    describe what the shot *needs*, not which model to use. Model selection is a
    router's job against structured capabilities; an LLM naming fal endpoints is
    how ``kling_2_5_turbo_pro`` came to render silently on Kling 2.1 Standard.
    """

    id: str                      # "s003.01" — beat id + zero-padded ordinal
    beat_id: str

    # Editorial intent
    purpose: str = ""            # establishing|master|reaction|insert|cutaway|detail|transition
    subject: str = ""
    shot_size: str = ""          # ews|ws|mw|m|mcu|cu|ecu
    angle: str = ""
    composition: str = ""

    # Timing + movement (single source of truth for duration)
    camera: Camera = field(default_factory=Camera)

    # Generation intent — router input
    character_motion: bool = False
    face_visibility: str = "none"      # none|low|moderate|high
    motion_complexity: str = "low"     # none|low|medium|high
    gestural: bool = False             # a movement that must complete; never trim it
    identity_critical: bool = False    # gets multiple takes
    reference_dependencies: list[str] = field(default_factory=list)

    # Resolved by the router (empty until then)
    motion_type: str = "parallax"
    backend: str = ""

    # Prompts
    prompt: str = ""
    motion_prompt: str = ""

    # Library reuse (no search subsystem — an explicit path only)
    source: str = "generated"          # generated|library
    source_ref: str = ""               # media-root-relative when source == library
    library_scope: str = ""            # series|project — recorded now, resolved later

    # Why this shot earns its place. The planner writes one sentence per shot;
    # without somewhere to keep it the justification was generated and discarded,
    # leaving a reviewer to infer intent from a framing and a duration.
    reason: str = ""

    # Provenance: why this shot is not what was originally wanted
    constrained_by: list[str] = field(default_factory=list)

    # Produced state
    draft_variations: list[str] = field(default_factory=list)
    chosen_variation: int | None = None
    clip: str = ""                     # render/<beat_id>/<id>.mp4
    # What fal was actually billed for, and for which inputs. `clip` cannot serve
    # this purpose: motion.render_shot writes the SAME path for a free parallax
    # render, and out_dir is render/<beat_id>/ -- a pure function of beat id and
    # shot index, both stable across re-plans, and nothing ever clears it. A guard
    # that only asked "is there a file at target?" therefore accepted a leftover
    # clip from a discarded plan, or a free render of a shot later promoted to
    # ai_video, as though it were the paid one: shipping the wrong footage and
    # never generating at all. That is a worse failure than the re-billing it
    # replaced, because it is silent.
    # Reference to the authoritative GenerationAttempt (backend/generation.py).
    # The shot points at its selected output rather than owning attempt history:
    # one clip and one error per shot means attempt two erases attempt one, and
    # §11.6 requires prior attempts to survive. paid_clip/paid_signature below
    # stay for now as the compatibility + re-bill fields, per the migration plan.
    selected_attempt: str = ""
    paid_clip: str = ""                # set ONLY when fal was billed
    paid_signature: str = ""           # the inputs that clip was bought for
    # FORWARD-LOOKING, and ONLY that. What producing this shot is expected to
    # cost — the number a human is quoted and consents to. Derived; see
    # quote_shot, the only thing that may write it.
    #
    # compile_coverage used to ADD its spend into this field, so a beat quoted at
    # $1.99 read $4.69 the moment it finished: 2.4× the number the human agreed
    # to, on a plan whose content had not changed by one shot, and the figure a
    # re-compile would then quote. A forward estimate and a record of spend are
    # different facts (§6.1 asks for "spent so far" and "estimated remaining"
    # separately) and one field cannot hold both without lying about both. What
    # was actually bought lives in the generation ledger, which is the only thing
    # that can also say what is merely AT RISK — see generation.spend.
    estimated_cost: float = 0.0
    error: str = ""

    @property
    def duration(self) -> float:
        return float(self.camera.duration) if self.camera else 0.0


@dataclass
class CoveragePlan:
    """The coverage for one beat, persisted beside the project."""

    beat_id: str
    beat_duration: float = 0.0     # SNAPSHOT of camera.duration when planned
    version: int = PLAN_VERSION
    plan_id: str = ""              # shared by beats planned together as a scene
    scene_beats: list[str] = field(default_factory=list)
    status: str = "draft"          # draft|locked|compiling|compiled|orphaned
    profile: str = ""
    created_by: str = "manual"     # manual|planner
    coverage: list[DirectorShot] = field(default_factory=list)
    # Critic findings. Annotated as list[str] for a long time while `critique`
    # had always written dicts -- which is why nothing type-checked the shape.
    warnings: list[dict] = field(default_factory=list)
    # Durable human disposition per warning id: {"decision", "note", "at"}.
    # A warning is only "handled" when a person recorded a decision about it;
    # dismissing one in the browser is not a decision the backend can see, and
    # locking a scene is not an answer to a specific finding. Contract §5.4:
    # unresolved Critic warnings must not be silently approved by bulk actions.
    warning_dispositions: dict = field(default_factory=dict)
    # Approval, bound to the exact plan it was given for. `status` says a human
    # acted; these say WHAT they acted on. Without the signature, "locked" is a
    # claim about the past that any later edit silently inherits.
    # The beat this plan was written against: its narration text and its
    # duration. beat_duration alone catches re-timing, but a rewritten line at
    # the same length moves silently past it, and the prompts were written for
    # the old line. Empty means "no baseline recorded" -- see beat_staleness.
    beat_signature: str = ""
    approved_signature: str = ""
    approved_at: str = ""
    approved_by: str = ""
    # Signatures of plans this one superseded, oldest first. History is kept so
    # an invalidated approval can be explained rather than merely lost.
    approval_history: list = field(default_factory=list)
    compiled: dict = field(default_factory=dict)
    # The scene's stated approach and its physical layout. Held per beat because
    # beats are the unit of storage, even though both are decided per scene.
    visual_strategy: str = ""
    blocking: dict = field(default_factory=dict)

    def total_duration(self) -> float:
        return sum(s.duration for s in self.coverage)



# --- plan identity and approval -------------------------------------------------
#
# `paid_signature` already establishes the pattern one level down: a stored
# artefact is only reusable while the inputs that determined it are unchanged.
# Approval needs exactly the same guarantee one level up. `status == "locked"`
# records that a human acted; on its own it cannot say what they acted on, so
# any later edit to the plan inherited the approval silently -- which is the
# approval drift contract §11.5 forbids.

# Fields that make a plan MATERIALLY different, i.e. that change what will be
# produced or what it costs. Deliberately excludes:
#   - produced state (clip, paid_clip, draft_variations, chosen_variation,
#     error): outputs of the plan, not the plan;
#   - chosen_variation in particular, because selecting a different take is
#     Generate's business (§10) and must not invalidate Director's approval;
#   - estimated_cost, which is derived from motion_type/backend/duration and
#     would double-count them;
#   - reason and constrained_by, which are rationale, not instruction.
_MATERIAL_SHOT_FIELDS = (
    "id", "beat_id", "purpose", "subject", "shot_size", "angle", "composition",
    "character_motion", "face_visibility", "motion_complexity", "gestural",
    "identity_critical", "reference_dependencies", "motion_type", "backend",
    "prompt", "motion_prompt", "source", "source_ref", "library_scope",
)

# Every DirectorShot field NOT in the material set, with the reason. This exists
# so the exclusion is a decision on the record rather than an omission: a test
# asserts the two tuples together account for the whole dataclass, so adding a
# field to DirectorShot fails until someone classifies it.
_NON_MATERIAL_SHOT_FIELDS = {
    "camera": "carried explicitly as camera_duration/camera_move",
    "reason": "rationale, not instruction",
    "constrained_by": "provenance for why intent was compromised",
    "estimated_cost": "derived from motion_type/backend/duration, already covered",
    "draft_variations": "produced state",
    "chosen_variation": "take selection belongs to Generate (contract §10)",
    "clip": "produced state",
    "selected_attempt": "reference to produced state, not an instruction",
    "paid_clip": "produced state; guarded by paid_signature",
    "paid_signature": "guards the clip, not the plan",
    "error": "produced state",
}

# Plan-level fields outside the signature, same discipline.
_NON_MATERIAL_PLAN_FIELDS = {
    "version": "schema version, not content",
    "plan_id": "groups beats planned together; provenance, not instruction",
    "scene_beats": "grouping, not instruction",
    "status": "lifecycle; the signature is what makes it trustworthy",
    "profile": "which planner produced it; provenance",
    "created_by": "provenance",
    "coverage": "carried explicitly, shot by shot",
    "warnings": "critic findings, gated separately by warning_dispositions",
    "warning_dispositions": "review record, not instruction",
    "beat_signature": "identifies the BEAT the plan was written against, not the plan",
    "approved_signature": "cannot be part of what it signs",
    "approved_at": "approval metadata",
    "approved_by": "approval metadata",
    "approval_history": "audit record",
    "compiled": "produced state",
    "visual_strategy": "stated approach; prose, does not reach the renderer",
    "blocking": "scene layout notes; does not reach the renderer today",
}


# Bumped whenever the material-field set or the encoding changes, so a stored
# signature can never be silently compared against one computed a different way.
SIGNATURE_VERSION = 2

# The shape plan_signature produces: sha256 hex, truncated to 16. Defined beside
# the version it belongs to, so a change to the hash has one place to update and
# signature_is_explicit cannot drift away from what plan_signature emits.
_SIGNATURE_RE = re.compile(r"[0-9a-f]{16}")


def material_plan(plan: "CoveragePlan") -> dict:
    """The plan reduced to what determines its output. The signature preimage.

    Structured rather than concatenated, and that is the point. The first
    version joined stringified fields with "|" and hashed the result, so a field
    value containing "|" shifted the boundaries: purpose="alpha|beta",
    subject="gamma" and purpose="alpha", subject="beta|gamma" produced identical
    signatures, and one plan's approval covered the other. Those are
    user-controlled strings, so it was reachable by typing. Keying every value
    to its field name removes the ambiguity entirely -- there are no boundaries
    left to shift.
    """
    return {
        "signature_version": SIGNATURE_VERSION,
        "beat_id": plan.beat_id,
        "beat_duration": round(float(plan.beat_duration), 3),
        "coverage": [
            {
                **{f: (list(getattr(ds, f, []) or [])
                       if isinstance(getattr(ds, f, None), list)
                       else getattr(ds, f, None))
                   for f in _MATERIAL_SHOT_FIELDS},
                "camera_duration": round(float(getattr(ds.camera, "duration", 0.0) or 0.0), 3),
                "camera_move": getattr(ds.camera, "move", None),
            }
            for ds in plan.coverage
        ],
    }


def plan_signature(plan: "CoveragePlan") -> str:
    """Identity of a plan as a PLAN: what it will produce, in what order.

    Two plans with the same signature are the same instruction set, so an
    approval of one is an approval of the other. Any material change produces a
    different signature and therefore invalidates the approval.
    """
    import hashlib
    import json as _json
    blob = _json.dumps(material_plan(plan), sort_keys=True,
                       separators=(",", ":"), ensure_ascii=False, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def signature_is_explicit(value: object) -> bool:
    """Is ``value`` a plan signature a caller actually sent? Contract §5.4.

    Sixteen lowercase hex characters, and nothing else -- the exact shape
    :func:`plan_signature` produces. Not truthiness. Truthiness is the defect
    this replaces, and it is the same defect
    :func:`backend.manifest.approval_is_explicit` was written for one merge
    earlier: the compile route read ``if plan_signature and plan_signature !=
    current``, so an omitted or empty value skipped the comparison entirely and
    an unsigned request dispatched whatever plan happened to be on disk. There,
    a truthy string cleared a gate; here, a falsy one did. Both made enforcement
    conditional on the shape of the input, and both let a caller opt out by
    supplying nothing.

    A missing signature is not agreement. It is an unanswered question, and the
    answer to an unanswered question about spending money is no.

    Malformed fails for the same reason ``"true"`` fails over there: a caller
    that sends something which is not a signature does not know what a signature
    is, and nothing here can tell what else it got wrong. There is no partial
    credit -- a value is the identity of a plan a human approved, or it is not.

    The direction of error is not symmetric and that is the whole design. A
    legitimate caller refused for want of a signature loses one request and is
    told exactly what to send. An unsigned request accepted buys paid video on
    a plan nobody agreed to.
    """
    return isinstance(value, str) and _SIGNATURE_RE.fullmatch(value) is not None


def beat_signature(beat) -> str:
    """Identity of the beat a plan was written against.

    Narration text and duration together. ``validate`` already refuses a plan
    whose beat has been re-timed, so the duration here is belt-and-braces; the
    text is the part nothing else can see. A line rewritten to the same length
    leaves every prompt in the plan describing something that is no longer being
    said, and the old code would have compiled it without comment.
    """
    import hashlib
    text = " ".join((getattr(beat, "narration", "") or "").split())
    dur = float(getattr(beat.camera, "duration", 0.0) or 0.0) if getattr(beat, "camera", None) else 0.0
    return hashlib.sha256(f"{text}|{dur:.3f}".encode("utf-8")).hexdigest()[:16]


def beat_staleness(plan: "CoveragePlan", beat) -> dict | None:
    """Whether the beat moved under a plan. None when it did not.

    Returns ``{"kind", "detail"}`` where kind is narration | timing | both.

    A plan with no recorded baseline reports None rather than "stale": it
    predates this check, and the original narration is not recoverable, so
    calling it stale would be a guess. Locking it records a baseline and it
    behaves normally from then on. Inventing the answer is worse than admitting
    the plan has none.
    """
    if not plan.beat_signature or beat is None:
        return None
    if plan.beat_signature == beat_signature(beat):
        return None

    live_dur = float(getattr(beat.camera, "duration", 0.0) or 0.0) if getattr(beat, "camera", None) else 0.0
    retimed = abs(float(plan.beat_duration) - live_dur) > DURATION_TOLERANCE
    # The text is what changed if the duration did not; if both moved, say so.
    kind = "both" if retimed else "narration"
    if retimed and plan.beat_signature == "":
        kind = "timing"
    detail = {
        "narration": (f"{plan.beat_id}: the narration was rewritten since this plan "
                      f"was made; its prompts describe the old line."),
        "timing": (f"{plan.beat_id}: the beat is now {live_dur:.2f}s, not "
                   f"{plan.beat_duration:.2f}s."),
        "both": (f"{plan.beat_id}: the narration was rewritten and the beat is now "
                 f"{live_dur:.2f}s, not {plan.beat_duration:.2f}s."),
    }[kind]
    return {"kind": kind, "detail": detail,
            "planned_for": plan.beat_signature, "beat_now": beat_signature(beat)}


def approve(plan: "CoveragePlan", by: str = "human", beat=None) -> str:
    """Bind an approval to the plan as it stands. Returns the signature.

    Passing the beat records what the plan was approved *against*, so a later
    rewrite of that narration is detectable. Re-locking is therefore also how a
    stale plan is accepted: the human has looked at the new line and said yes.
    """
    import datetime as _dt
    sig = plan_signature(plan)
    if plan.approved_signature and plan.approved_signature != sig:
        plan.approval_history = list(plan.approval_history or []) + [{
            "signature": plan.approved_signature,
            "approved_at": plan.approved_at,
            "approved_by": plan.approved_by,
            "superseded_by": sig,
        }]
    plan.approved_signature = sig
    plan.approved_at = _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")
    plan.approved_by = by
    if beat is not None:
        plan.beat_signature = beat_signature(beat)
    return sig


def approval_is_current(plan: "CoveragePlan") -> bool:
    """Whether this plan carries an approval for the plan it actually is now."""
    return bool(plan.approved_signature) and plan.approved_signature == plan_signature(plan)


def invalidate_approval(plan: "CoveragePlan", reason: str = "plan changed") -> bool:
    """Drop an approval the plan has outgrown, keeping the record of it.

    Returns True when an approval was actually invalidated. The plan returns to
    draft: a materially different plan has not been approved by anyone, and
    leaving it "locked" is precisely the drift this guards against.
    """
    if not plan.approved_signature or approval_is_current(plan):
        return False
    plan.approval_history = list(plan.approval_history or []) + [{
        "signature": plan.approved_signature,
        "approved_at": plan.approved_at,
        "approved_by": plan.approved_by,
        "invalidated_because": reason,
        "superseded_by": plan_signature(plan),
    }]
    plan.approved_signature = ""
    plan.approved_at = ""
    plan.approved_by = ""
    if plan.status in ("locked", "compiled"):
        plan.status = "draft"
    return True


# --- critic warnings ------------------------------------------------------------
#
# A warning needs a stable identity before a human decision about it can be
# stored. It is derived from the warning's CONTENT, deliberately: re-running the
# critic on an unchanged plan reproduces the same ids, so a recorded decision
# survives a re-critique -- while a warning whose text or target changed gets a
# new id and therefore needs deciding again. Clearing a finding must never clear
# a different one that merely occupies the same position in a list.

def warning_id(w: dict) -> str:
    """Stable id for a critic warning, derived from what it says."""
    import hashlib
    parts = [str(w.get("beat_id") or ""), str(w.get("shot_id") or ""),
             str(w.get("kind") or ""), str(w.get("detail") or "")]
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:12]


def normalize_warnings(raw: list | None) -> list[dict]:
    """Coerce stored warnings to dicts carrying stable, DERIVED ids.

    The id is always recomputed from the identity-bearing content and an
    incoming ``id`` is never trusted. Honouring a supplied id would let a
    changed finding keep the identity of the one it replaced, and therefore
    inherit the human decision recorded against that older finding -- so a new
    problem would arrive pre-approved and the scene would lock straight over it.
    Identity has to be a property of what the warning says, not of who is
    handing it to us.

    Any supplied id is preserved as ``source_id`` for tracing. It is never the
    disposition key.

    Migrate-on-read, and idempotent: derived from data that is already there,
    never invented, so re-loading a plan cannot change it. Legacy plans that
    stored bare strings keep their text under ``detail``.
    """
    out: list[dict] = []
    for w in (raw or []):
        d = dict(w) if isinstance(w, dict) else {"kind": "legacy", "detail": str(w)}
        supplied = str(d.pop("id", "") or "")
        d["id"] = warning_id(d)
        if supplied and supplied != d["id"]:
            d["source_id"] = supplied
        out.append(d)
    return out


def unresolved_warnings(plan: "CoveragePlan") -> list[dict]:
    """Warnings with no recorded human decision. These block locking."""
    disp = plan.warning_dispositions or {}
    return [w for w in normalize_warnings(plan.warnings)
            if not (disp.get(w["id"]) or {}).get("decision")]


def resolve_warning(plan: "CoveragePlan", wid: str, decision: str,
                    note: str = "", by: str = "human") -> dict:
    """Record a human decision about one warning.

    ``decision`` is "resolved" (the plan was changed to answer it) or
    "accepted" (understood and deliberately kept). Both are decisions; neither
    is silence. Passing "" clears the disposition again.
    """
    known = {w["id"] for w in normalize_warnings(plan.warnings)}
    if wid not in known:
        raise PlanError(f"{plan.beat_id}: no warning {wid} on this plan")
    if decision and decision not in ("resolved", "accepted"):
        raise PlanError(f"unknown decision {decision!r}; use resolved|accepted")
    disp = dict(plan.warning_dispositions or {})
    if decision:
        disp[wid] = {"decision": decision, "note": note or "", "by": by}
    else:
        disp.pop(wid, None)
    plan.warning_dispositions = disp
    return disp.get(wid, {})


# --- review triage --------------------------------------------------------------
#
# Reviewing 158 shots one at a time is not a workflow, so the frontend triages
# them. That triage MUST be computed here, once. A client-side copy of this rule
# would drift from the server's, and this codebase has already shipped two
# registries duplicated into the UI that quietly disagreed with the backend.

TIER_STANDARD = 1     # free, unremarkable, safe to accept in bulk
TIER_CHECK = 2        # spends money, or a technical limit changed the shot
TIER_CREATIVE = 3     # needs a human's taste, not their approval


def shot_tier(ds: "DirectorShot", warnings: list[dict] | None = None) -> dict:
    """Which review tier a shot falls in, and why.

    The reason is returned alongside the number because "tier 2" on its own tells
    a reviewer nothing, and a tier they cannot interrogate is one they will learn
    to click past.
    """
    flagged = [w for w in (warnings or []) if w.get("shot_id") == ds.id]
    if ds.identity_critical:
        return {"tier": TIER_CREATIVE, "reason": "identity anchor — pick the take"}
    if ds.face_visibility in ("moderate", "high"):
        return {"tier": TIER_CREATIVE,
                "reason": f"face visible ({ds.face_visibility}) and identity is unmeasured"}
    if flagged:
        return {"tier": TIER_CREATIVE,
                "reason": flagged[0].get("kind") or "flagged by the critic"}
    if ds.constrained_by:
        return {"tier": TIER_CHECK,
                "reason": "changed by a technical limit: " + ", ".join(ds.constrained_by)}
    if ds.motion_type == "ai_video":
        return {"tier": TIER_CHECK, "reason": "paid generation"}
    return {"tier": TIER_STANDARD, "reason": "standard free coverage"}


def triage(plan: "CoveragePlan") -> dict:
    """Tier every shot in a plan, with the cost sitting in each tier.

    Cost per tier matters more than the counts. Free coverage is the *largest*
    line item in a film -- every parallax shot still needs a still -- so a UI that
    labels tier 1 "$0, no clicks" hides the majority of the spend behind the
    reassuring colour.
    """
    out = {TIER_STANDARD: [], TIER_CHECK: [], TIER_CREATIVE: []}
    for ds in plan.coverage:
        t = shot_tier(ds, plan.warnings)
        out[t["tier"]].append((ds, t["reason"]))
    return {
        "tiers": {
            str(k): {
                "shots": len(v),
                "cost": round(sum(ds.estimated_cost for ds, _ in v), 2),
                "ids": [ds.id for ds, _ in v],
            } for k, v in out.items()
        },
        "needs_review": [ds.id for ds, _ in out[TIER_CHECK] + out[TIER_CREATIVE]],
    }

# --- persistence ---------------------------------------------------------------
#
# One file per beat, outside the manifest. Small, independently writable, and
# deletable — which is what makes this whole experiment reversible.

# Kept as the module-level names the approval tests exercise; the behaviour now
# lives in backend.atomic so plans and generation lineage share one implementation.
_replace_with_retry = atomic.replace_with_retry
_REPLACE_ATTEMPTS = atomic._ATTEMPTS
_REPLACE_BACKOFF = atomic._BACKOFF


def director_dir() -> Path:
    return config.project_dir() / "director"


def plan_path(beat_id: str) -> Path:
    return director_dir() / f"{beat_id}.json"


def load_plan(beat_id: str) -> CoveragePlan | None:
    p = plan_path(beat_id)
    if not p.is_file():
        return None
    # Through backend.atomic, not a bare read: this function persists the
    # transitions below, so every reader here is also a writer and a bare open
    # raced the replace.
    raw = atomic.read_json(p)
    shots = []
    for d in raw.get("coverage") or []:
        cam = Camera(**(d.pop("camera", None) or {}))
        d.pop("duration", None)  # tolerate hand-authored files that add it
        shots.append(DirectorShot(camera=cam, **d))
    raw["coverage"] = shots
    raw.pop("version", None)
    # Migrate-on-read: stamp ids onto warnings that predate them. Derived from
    # content that is already present, so this is idempotent and invents nothing.
    raw["warnings"] = normalize_warnings(raw.get("warnings"))
    raw["warning_dispositions"] = dict(raw.get("warning_dispositions") or {})
    plan = CoveragePlan(version=PLAN_VERSION, **raw)

    # Migrate-on-read for plans locked before approvals carried a signature.
    # Their approval is derivable from authoritative state -- the plan is locked
    # and this is the plan it was locked on -- so it is adopted rather than
    # invented, and stamped with provenance saying so. Nothing else gets one:
    # a draft has never been approved, and inventing a signature for it would
    # manufacture the approval the signature exists to prove.
    changed = False

    # Migrate-on-read: estimated_cost is DERIVED, so derive it. Plans compiled
    # before spend had its own field carry a figure the compile added its spend
    # into -- the beat quoted at $1.99 reads $4.69 on disk right now -- and the
    # doubling would otherwise survive this fix for the life of every existing
    # plan. Recomputing invents nothing: quote_shot reads only motion_type,
    # backend, duration, gestural and identity_critical, all of which are in the
    # file, and estimated_cost is outside the signature (see
    # _NON_MATERIAL_SHOT_FIELDS) so no approval moves. Idempotent: the second
    # read finds the same number and writes nothing.
    #
    # IT REPAIRS DOWNWARD TOO, and that is deliberate rather than overlooked.
    # The objection is real in general -- a stale table could pull a deliberately
    # conservative figure down -- but it does not apply here, for two reasons and
    # one caveat:
    #
    #   1. There is no sanctioned way for a human to set this field. The shot
    #      patch route strips estimated_cost from the request body as "a field
    #      the SERVER owns" (main.py), so a raised figure is not something the
    #      product can produce. Refusing to lower a value would protect a case
    #      that cannot arise through the UI.
    #   2. The value this exists to repair is INFLATED -- $4.69 against $1.99.
    #      A never-lower rule would decline to fix the exact plan it was written
    #      for, which is the whole defect left in place under a safety argument.
    #
    # The caveat: a hand-edited plan file CAN carry a raised number, and this
    # will flatten it. That is the accepted cost. If a human override is ever
    # wanted it needs its own field with its own provenance -- who raised it and
    # why -- because an override that is indistinguishable from a stale
    # derivation is not an override, it is a number nobody can explain.
    for ds in plan.coverage:
        fresh = quote_shot(ds)
        if abs(fresh - float(ds.estimated_cost or 0.0)) >= 0.005:
            ds.estimated_cost = fresh
            changed = True

    if plan.status in ("locked", "compiling", "compiled") and not plan.approved_signature:
        plan.approved_signature = plan_signature(plan)
        plan.approved_by = "migrated:pre-signature-lock"
        changed = True

    # A plan whose file was edited after approval arrives here already drifted.
    # Catching it on read means every consumer sees the same truth, rather than
    # each having to remember to check.
    if invalidate_approval(plan, reason="plan changed after approval"):
        changed = True

    # Persist the transition. Doing this only in memory left the FILE asserting
    # a stale locked status with a signature that no longer matched, so anything
    # not reading through this function still saw the false approval and the
    # invalidation history was rebuilt from scratch on every read instead of
    # being recorded once. The write is conditional, so it happens on the read
    # that actually transitions and never again -- which is what makes it
    # idempotent rather than a write on every load.
    if changed:
        try:
            save_plan(plan)
        except OSError as exc:
            # A read must not fail because the disk did, and the object returned
            # here is the SAFE one: an invalidated approval has already been
            # demoted to draft in memory, so nothing can compile on the stale
            # approval even though the file still asserts it. Loud, because the
            # file and the running process now disagree.
            print(f"director: FAILED to persist approval transition for "
                  f"{plan.beat_id}; the file still asserts the old state: {exc}")
    return plan


def save_plan(plan: CoveragePlan) -> Path:
    """Write a plan durably. The write rules live in backend.atomic.

    They were here first, learned from S2-03: a unique temp per write, a lock
    per destination, and bounded retry on the replace because Windows denies it
    transiently even when this process holds the only logical lock. They moved
    so generation lineage gets the same guarantees rather than a second copy of
    them that drifts.
    """
    director_dir().mkdir(parents=True, exist_ok=True)
    return atomic.write_json(plan_path(plan.beat_id), asdict(plan))


def has_locked_coverage(beat_id: str) -> bool:
    """Whether a normal beat render would be overwriting director work."""
    plan = load_plan(beat_id)
    return bool(plan and plan.status in ("locked", "compiling", "compiled"))


# --- validation ----------------------------------------------------------------

class PlanError(RuntimeError):
    """A plan that cannot compile. Raised before anything is generated."""


def validate(plan: CoveragePlan, beat: Shot) -> None:
    """Refuse a plan that cannot produce a correct beat clip.

    Every check here runs *before* generation, because the expensive failure is
    discovering after paying for seven shots that they do not add up.
    """
    if not plan.coverage:
        raise PlanError(f"{plan.beat_id}: plan has no coverage")

    beat_dur = float(beat.camera.duration) if beat.camera else 0.0

    # The snapshot check. The plan was written against a duration; if narration
    # was re-recorded since, that duration moved and the coverage no longer fills
    # the beat. A preview once reported "complete" while being 81 seconds short
    # because nothing compared what was built against what was current.
    if abs(plan.beat_duration - beat_dur) > DURATION_TOLERANCE:
        raise PlanError(
            f"{plan.beat_id}: plan was made for {plan.beat_duration:.2f}s but the beat "
            f"is now {beat_dur:.2f}s — narration changed, replan this beat"
        )

    total = plan.total_duration()
    if abs(total - beat_dur) > DURATION_TOLERANCE:
        raise PlanError(
            f"{plan.beat_id}: coverage totals {total:.2f}s but the beat is {beat_dur:.2f}s "
            f"({total - beat_dur:+.2f}s) — coverage must fill the beat exactly"
        )

    ids = [s.id for s in plan.coverage]
    if len(set(ids)) != len(ids):
        raise PlanError(f"{plan.beat_id}: duplicate director shot ids: {ids}")

    for s in plan.coverage:
        if s.duration <= 0:
            raise PlanError(f"{s.id}: duration must be > 0")
        if s.source == "library" and not s.source_ref:
            raise PlanError(f"{s.id}: source=library needs a source_ref")
        if s.source == "generated" and not s.prompt:
            raise PlanError(f"{s.id}: generated shots need a prompt")


# --- media ---------------------------------------------------------------------

def _ffmpeg() -> str:
    """ffmpeg is a system install here, never a pip dependency (see CLAUDE.md)."""
    return ffmpeg_bin()


def _ffprobe() -> str:
    return ffprobe_bin()


def probe_seconds(path: Path) -> float:
    out = subprocess.run(
        [_ffprobe(), "-v", "error", "-show_entries", "format=duration",
         "-of", "default=nw=1:nk=1", str(path)],
        capture_output=True, text=True, timeout=30,
    )
    return float((out.stdout or "0").strip() or 0.0)


def probe_stream(path: Path) -> dict:
    """Codec / size / fps / pixel format, for the conformance check."""
    out = subprocess.run(
        [_ffprobe(), "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=codec_name,width,height,pix_fmt,r_frame_rate",
         "-of", "json", str(path)],
        capture_output=True, text=True, timeout=30,
    )
    try:
        st = (json.loads(out.stdout or "{}").get("streams") or [{}])[0]
    except (json.JSONDecodeError, IndexError):
        return {}
    rate = st.get("r_frame_rate") or "0/1"
    try:
        num, den = rate.split("/")
        fps = float(num) / float(den or 1)
    except (ValueError, ZeroDivisionError):
        fps = 0.0
    return {
        "codec": st.get("codec_name"), "width": st.get("width"),
        "height": st.get("height"), "pix_fmt": st.get("pix_fmt"), "fps": round(fps, 3),
    }


def is_canonical(path: Path) -> bool:
    s = probe_stream(path)
    return bool(s) and (
        s.get("codec") == "h264"
        and s.get("height") == CANON_HEIGHT
        and s.get("width") == CANON_WIDTH      # width matters as much as height
        and s.get("pix_fmt") == CANON_PIXFMT
        and abs(float(s.get("fps") or 0) - CANON_FPS) < 0.01
    )


def normalize_clip(path: Path, log=print) -> Path:
    """Force a clip to the canonical stream parameters, in place.

    This is the requirement the ``-c copy`` concat imposes and that nothing
    currently guarantees. A locally-rendered clip is already conformant and is
    left alone after a probe; anything from fal or a library asset is re-encoded.

    Writes to a temp name and copies over the target — ``shutil.copyfile``, not
    ``rename``, because gcsfuse rejects the metadata operations rename and
    ``copy2`` imply and raises ``[Errno 1] Operation not permitted``.
    """
    path = Path(path)
    if is_canonical(path):
        return path
    tmp = path.with_name(f"{path.stem}__norm.mp4")
    # Fit inside the canvas, then pad to it exactly. An aspect-preserving
    # `scale=-2:720` is NOT enough: a source that is not precisely 16:9 lands at
    # 1278x720 or similar, and mixed widths break `-c copy` just as surely as
    # mixed heights. Found by dry run — the first build produced both 1278 and
    # 1280 in one file.
    vf = (f"scale={CANON_WIDTH}:{CANON_HEIGHT}:force_original_aspect_ratio=decrease,"
          f"pad={CANON_WIDTH}:{CANON_HEIGHT}:(ow-iw)/2:(oh-ih)/2,"
          f"fps={CANON_FPS},setsar=1")
    subprocess.run(
        [_ffmpeg(), "-y", "-v", "error", "-i", str(path),
         "-vf", vf,
         "-c:v", "libx264", "-crf", CANON_CRF, "-pix_fmt", CANON_PIXFMT,
         "-preset", "medium", "-an", str(tmp)],
        check=True,
    )
    shutil.copyfile(tmp, path)
    tmp.unlink(missing_ok=True)
    log(f"  normalized {path.name} to {CANON_HEIGHT}p/{CANON_FPS}fps")
    return path


def fit_clip(path: Path, want: float, gestural: bool = False, log=print) -> None:
    """Make a clip exactly ``want`` seconds.

    Short clips are padded by freezing the final frame, as beats already are.
    Long clips are trimmed from the tail — but only when the motion is ambient.
    A generated clip of a *gesture* is one designed movement across its whole
    duration, so trimming it stops the head mid-turn or the hand short of the
    object; that reads as broken, where a few tenths of a second of rhythm does
    not. A gestural overrun is reported instead, for the router to solve by
    picking a free-duration model.
    """
    have = probe_seconds(path)
    if have <= 0:
        raise PlanError(f"{path.name}: unreadable clip")
    delta = want - have
    if abs(delta) < 0.04:                      # under one frame at 24fps
        return

    if delta > 0:
        tmp = path.with_name(f"{path.stem}__fit.mp4")
        subprocess.run(
            [_ffmpeg(), "-y", "-v", "error", "-i", str(path),
             "-vf", f"tpad=stop_mode=clone:stop_duration={delta:.3f}",
             "-c:v", "libx264", "-crf", CANON_CRF, "-pix_fmt", CANON_PIXFMT, "-an",
             str(tmp)],
            check=True,
        )
        shutil.copyfile(tmp, path)
        tmp.unlink(missing_ok=True)
        log(f"  {path.name}: padded {have:.2f}s -> {want:.2f}s (froze final frame)")
        return

    if gestural and abs(delta) > GESTURAL_TRIM_TOLERANCE:
        raise PlanError(
            f"{path.name}: is {have:.2f}s but the shot wants {want:.2f}s "
            f"({abs(delta):.2f}s over), and it is marked gestural so it must not be "
            f"trimmed mid-movement — regenerate it at a legal duration for this "
            f"model, or clear `gestural`"
        )
    tmp = path.with_name(f"{path.stem}__fit.mp4")
    subprocess.run(
        [_ffmpeg(), "-y", "-v", "error", "-i", str(path), "-t", f"{want:.3f}",
         "-c:v", "libx264", "-crf", CANON_CRF, "-pix_fmt", CANON_PIXFMT, "-an",
         str(tmp)],
        check=True,
    )
    shutil.copyfile(tmp, path)
    tmp.unlink(missing_ok=True)
    log(f"  {path.name}: trimmed {have:.2f}s -> {want:.2f}s (ambient motion)")


def concat(clips: list[Path], dest: Path, log=print) -> Path:
    """Join sub-clips with stream copy. Order is the caller's, never a glob.

    Builds to a temp file and copies over ``dest`` only on success, so a failure
    leaves the previous beat clip intact rather than half-written.
    """
    missing = [c for c in clips if not Path(c).is_file()]
    if missing:
        raise PlanError(f"cannot concat, missing clips: {[m.name for m in missing]}")

    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    listing = dest.parent / f"_{dest.stem}_concat.txt"
    listing.write_text(
        "".join(f"file '{Path(c).resolve().as_posix()}'\n" for c in clips),
        encoding="utf-8",
    )
    tmp = dest.with_name(f"{dest.stem}__build.mp4")
    try:
        subprocess.run(
            [_ffmpeg(), "-y", "-v", "error", "-f", "concat", "-safe", "0",
             "-i", str(listing), "-c", "copy", str(tmp)],
            check=True,
        )
        shutil.copyfile(tmp, dest)
        log(f"  concatenated {len(clips)} clips -> {dest.name}")
    finally:
        tmp.unlink(missing_ok=True)
        listing.unlink(missing_ok=True)
    return dest


# --- compilation ---------------------------------------------------------------

def library_root() -> Path:
    """Where series-level reusable shots live.

    Round 4 asked for both scopes. Project-scoped assets resolve through the
    normal media resolver; *series*-scoped ones cannot, because they sit outside
    any one project directory — which is precisely the point, since an
    establishing shot of a location should amortise across every episode that
    visits it. This follows ``config.AUDIO_POOL``: shared assets live at the
    storage root, on the bucket when deployed, so they survive the container and
    are visible to every project.
    """
    mount = Path("/gcs")
    base = mount if mount.exists() else config.ROOT
    return base / "shot_library"


def resolve_library_ref(ref: str) -> Path | None:
    """Resolve a library reference across both scopes, most specific first.

    Order: an explicit path as given, then project-scoped media, then the series
    library. Returning ``None`` rather than guessing keeps a missing asset a
    loud failure instead of a silently substituted one.
    """
    if not ref:
        return None
    direct = Path(ref)
    if direct.is_file():
        return direct
    scoped = config.resolve_media(ref, None)
    if scoped and Path(scoped).is_file():
        return Path(scoped)
    series = library_root() / ref
    if series.is_file():
        return series
    return None


def _media_present(rel: str) -> bool:
    """Whether a recorded output is really usable media.

    Existence is not enough: a truncated download leaves a zero-byte file that
    is_file() happily confirms, and reusing it ships an empty clip while
    reporting a hit. The re-bill guard has always tested size for this reason,
    and the attempt-level reuse check has to be exactly as strict or it becomes
    the weaker of the two and decides first.
    """
    try:
        path = config.resolve_media(rel)
        return bool(path) and path.is_file() and path.stat().st_size > 0
    except OSError:
        return False


# --- price: one source for the quote and for the record -------------------------
#
# There used to be a flat ``PAID_CLIP_COST = 0.60`` here, and it was the figure
# recorded on the attempt. The figure QUOTED to the human came from somewhere
# else entirely — capabilities' per-second table, via planner.plan_coverage. The
# two were never reconciled, so the first real end-to-end compile put "COMPILE &
# SPEND $1.99" on the button and wrote $0.60 per paid shot to a ledger that had
# quoted $0.40 and $0.39 for them.
#
# The functions below are now the only way either side gets a number, so they
# cannot disagree by construction. The bound itself lives in
# capabilities.clip_price; what lives here is the shot-shaped question — how many
# stills does this shot buy, and which model will actually serve it.


def still_takes(ds: "DirectorShot") -> int:
    """How many draft stills this shot buys.

    One place, because compile_coverage SPENDS this many and the quote has to
    name the same number. It did not: the quote was a flat one image per shot
    while an ``identity_critical`` shot has always bought four, so every such
    shot was quoted $0.15 against $0.60 of stills — the same understatement as
    the video tier, at 4×, on the shots that matter most.
    """
    return 4 if ds.identity_critical else 1


def routed_clip(ds: "DirectorShot") -> dict:
    """The model that will actually serve this shot's paid clip.

    Used by the quote and by generate_paid_clip, including the fallback off a
    stale recorded backend. If the two resolved differently the quote would price
    one model and the compile would buy another, which is the defect one level up
    from the one this module just fixed.
    """
    from . import capabilities

    intent = {"duration": ds.duration, "gestural": ds.gestural}
    routed = capabilities.resolve(intent, prefer=[ds.backend] if ds.backend else None)
    if not routed.get("backend") and ds.backend:
        # The recorded model cannot serve this shot any more -- usually because a
        # capability entry was corrected after the plan was written. Re-resolve
        # across everything rather than failing on a stale choice.
        routed = capabilities.resolve(intent)
    return routed


def paid_clip_price(ds: "DirectorShot") -> float | None:
    """What this shot's clip is quoted at, and therefore recorded at.

    ``None`` — not zero — when no configured model can produce the shot at all.
    A confident $0.00 is the one answer money must never get, and the caller
    needs the distinction: the quote leaves the clip out because nothing will be
    bought, while the compile refuses before it opens a paid attempt.
    """
    routed = routed_clip(ds)
    if not routed.get("backend"):
        return None
    return float(routed.get("estimated_cost") or 0.0)


def unproducible(ds: "DirectorShot") -> str:
    """Why this paid shot cannot be generated, or "" if it can."""
    return (f"{ds.id}: no configured model can produce {ds.duration:.2f}s"
            + (" without trimming a gesture" if ds.gestural else "")
            + f". {routed_clip(ds).get('reason', '')}")


def quote_shot(ds: "DirectorShot") -> float:
    """What producing this shot is expected to cost. The number consent is given for.

    Every term is a term compile_coverage will actually spend, priced by the same
    function that will write it to the generation ledger. The only permitted
    writer of ``DirectorShot.estimated_cost``.
    """
    from . import capabilities

    price = capabilities.COST_PER_IMAGE * still_takes(ds)
    if ds.motion_type == "ai_video":
        clip = paid_clip_price(ds)
        # Nothing is bought for a shot no model can serve -- compile_coverage
        # refuses it before opening an attempt -- so quoting for it would be a
        # charge that cannot happen.
        if clip is not None:
            price += clip
    # 4dp, the same as capabilities.clip_price and estimate_shot_cost -- see
    # the note there. A still now costs $0.0398 and 3dp quietly rounds it up.
    return round(price, 4)


def generate_paid_clip(ds: DirectorShot, synth: Shot, sb: Storyboard,
                       out_dir: Path, log=print, on_billed=None) -> Path:
    """Generate one Tier-C coverage clip from its own still.

    Deliberately *not* a capability router — that is Spike C. This resolves the
    endpoint from the registry, asks for the shot's own duration, and downloads
    the result. Coverage shots are 3-5 seconds, which is inside every model's
    native range, so the extension and frozen-frame padding that a 20-35 second
    beat forces are simply not needed here. That is the point: a short shot is
    the *cheap* way to buy real motion, not the expensive one.

    No cross-beat chaining. Continuity inside a beat comes from the shots sharing
    one still lineage and one grade; ``native_extend`` exists to bridge a cut
    between beats and would only add a dependency between sibling shots that
    must be renderable in any order.

    Audio is always off. The beat's narration, SFX and music are mixed separately
    by ``timeline.build_preview``, and every other clip in the pipeline is silent
    — a sub-clip arriving with its own audio track would be both wasted spend and
    a stream mismatch at concat.

    ``on_billed``, if given, is called once with what fal says it billed for this
    request — see ``backend.fal_billing``. It is a callback rather than a second
    return value because the measurement is strictly optional: fal may not report
    one, and this function's contract is "the clip, or an exception", which a
    missing price must not disturb.
    """
    import fal_client

    from . import assets

    from . import capabilities
    from . import fal_billing

    want = float(ds.duration)

    # Go through the router rather than trusting the backend recorded at plan
    # time. This function previously resolved the model itself and applied its own
    # max(3, min(10, ...)) -- the exact hardcoded clamp Spike C exists to replace --
    # so the capability table was consulted when PLANNING and ignored when
    # GENERATING. That is how a 3.34s gestural shot asked wan_2_7 for 3s, received
    # a fixed 5s clip, and then could not be trimmed.
    routed = routed_clip(ds)
    if not routed.get("backend"):
        raise PlanError(unproducible(ds))

    key = routed["backend"]
    endpoint = assets.resolve_video_backend(key)["endpoint"]
    dur_int = int(routed["generate_seconds"])
    if key != (ds.backend or key):
        log(f"  routed {ds.id} to {key} (plan said {ds.backend!r})")
    ds.backend = key
    for c in routed.get("constraints") or []:
        if c not in ds.constrained_by:
            ds.constrained_by.append(c)

    prompt = ds.motion_prompt or f"Cinematic motion, authentic detail, {ds.prompt}"
    if f"{dur_int}s" not in prompt and "second" not in prompt:
        prompt = f"{prompt} (duration: ~{dur_int} seconds)"

    # Each endpoint spells duration its own way; capabilities knows which.
    dur_arg = capabilities.duration_argument(key, want)
    arguments: dict = {"prompt": prompt, "generate_audio": False}
    if dur_arg is not None:
        arguments["duration"] = dur_arg
    # The per-endpoint special-casing that used to live here (a veo branch, a
    # seedance check) is exactly what the schema-derived table replaces.

    still = synth.draft_image or (synth.draft_variations or [None])[0]
    local_still = config.resolve_media(still, synth.scene_id) if still else None
    if not local_still or not Path(local_still).is_file():
        raise PlanError(f"{ds.id}: no still to drive the video from")

    log(f"  PAID video for {ds.id} via {endpoint} ({dur_int}s from {Path(local_still).name})")
    arguments["image_url"] = fal_client.upload_file(str(local_still))

    # The request id is fal's handle on the thing we are about to be charged
    # for, and `subscribe` does not return it. Without it there is no way to ask
    # fal afterwards what it billed, and no way for a human to find this clip's
    # line on their fal dashboard. It is captured here, before the call can
    # fail, so even a request that dies mid-flight leaves the id behind.
    request_ids: list[str] = []
    result = fal_client.subscribe(endpoint, arguments=arguments, with_logs=False,
                                  on_enqueue=request_ids.append)
    url = (result.get("video") or {}).get("url") or (result.get("file") or {}).get("url")
    if not url:
        raise PlanError(f"{ds.id}: no video URL returned by {endpoint}")

    dest = out_dir / f"{ds.id}.mp4"
    assets._download(url, dest)
    log(f"  downloaded {dest.name}")

    if on_billed is not None and request_ids:
        # Last, and deliberately so. Getting the clip in hand is the thing that
        # was paid for; asking what it cost is optional and must not delay it. A
        # re-fetch of a completed result is free, and measure_quietly swallows
        # its own failures, so the worst case here is that the attempt records
        # our estimate exactly as it did before this existed.
        billed = fal_billing.measure_quietly(endpoint, request_ids[0], log=log)
        if billed is not None:
            log(f"  fal billed {billed['units']} {billed['unit']} "
                f"× ${billed['unit_price']} = ${billed['cost']:.4f} "
                f"(we estimated ${capabilities.clip_price(key, dur_int):.4f})")
        on_billed(billed)

    return dest



def character_in_shot(ds: "DirectorShot") -> tuple[str, str]:
    """Which known character this shot shows, and their likeness reference.

    Detected by name in the subject or prompt, and by explicit
    reference_dependencies. Spike A measured the consequence of getting this
    wrong: with a likeness reference every take is the same documented man;
    without one, four takes give four different strangers. So a shot that names a
    character and does not carry their reference is not slightly worse, it is a
    different person.
    """
    try:
        from . import characters
        known = characters.load_characters()
    except Exception:  # noqa: BLE001
        return "", ""
    haystack = f"{ds.subject} {ds.prompt}".lower()
    for name, spec in (known or {}).items():
        named = name.lower() in haystack or name in (ds.reference_dependencies or [])
        if not named:
            continue
        ref = (spec or {}).get("reference_image") or ""
        local = config.resolve_media(ref) if ref else None
        return name, (str(local) if local else "")
    return "", ""

def _synthetic_shot(ds: DirectorShot, beat: Shot) -> Shot:
    """A throwaway ``Shot`` standing in for one Director Shot.

    ``motion.render_shot`` and ``assets.generate_for_shot`` both take a ``Shot``
    and derive their output paths from ``scene_id``. Handing them a synthetic one
    whose scene_id is the director shot id gets sub-clip rendering, depth,
    parallax, grade and prompt composition for free, with no signature changes
    and nothing written to the manifest.

    Style is inherited from the parent beat: coverage is *inside* one beat, so it
    must not drift in medium from shot to shot.
    """
    try:
        mt = MotionType(ds.motion_type)
    except ValueError:
        mt = MotionType.PARALLAX
    return Shot(
        scene_id=ds.id,
        narration="",                       # audio stays on the beat, never here
        prompt=ds.prompt,
        style_medium=getattr(beat, "style_medium", "") or "",
        motion_prompt=ds.motion_prompt,
        camera=ds.camera,
        motion_type=mt,
        references=list(getattr(beat, "references", None) or []),
        grade=dict(getattr(beat, "grade", None) or {}),
    )


def paid_signature(ds: "DirectorShot") -> str:
    """Identify the inputs a paid clip was bought for.

    A stored paid clip is only reusable if the things that determined its content
    are unchanged. Change the still, the motion prompt, the length or the model
    and the file on disk is no longer the clip this shot is asking for -- so it
    must be regenerated rather than silently reused.
    """
    import hashlib
    parts = [ds.id, str(ds.chosen_variation), ds.motion_prompt or ds.prompt or "",
             f"{ds.duration:.2f}", ds.backend or "",
             (ds.draft_variations or [""])[ds.chosen_variation or 0]
             if ds.draft_variations else ""]
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:16]


def compile_coverage(plan: CoveragePlan, sb: Storyboard, render_dir: Path,
                     backend: str | None = None, log=print,
                     skip_existing: bool = True) -> Path:
    """Render every Director Shot and assemble the beat clip.

    Resume-safe by the same rule as the rest of the pipeline: a shot that already
    has a current clip is skipped, so re-running after a failure costs only what
    failed. The plan is saved after each shot for the same reason.

    A shot whose LOCAL render fails is attempted once more before the beat is
    given up on — see ``LOCAL_RENDER_ATTEMPTS``. That never extends to a paid
    generation: the boundary is ``local_only`` in ``_compile_locked``, and it is
    set only where nothing left to run for that shot can be charged for.
    """
    # Imported lazily and per-branch: a plan made entirely of library assets, or a
    # resumed run whose shots are already rendered, has no business requiring the
    # render engine and its ML stack just to concatenate files.
    beat = next((s for s in sb.shots if s.scene_id == plan.beat_id), None)
    if beat is None:
        raise PlanError(f"{plan.beat_id}: no such beat in the manifest")

    validate(plan, beat)

    # Gate 1. Coverage is a new route to the paid video tier, and it was walking
    # straight past the approval gate that the ordinary render path honours:
    # compile_coverage calls generate_paid_clip for any ai_video shot, and nothing
    # here consulted storyboard_approved. CLAUDE.md is unambiguous that Tier C is
    # unreachable until the storyboard is approved, because approval is where the
    # human allocates the render budget -- and a coverage plan that spends on
    # generated video before that is exactly the spend the gate exists to stop.
    #
    # Free tiers stay open. Static and parallax cost nothing, and drafts are
    # explicitly a pre-gate activity, so an unapproved beat can still be assembled
    # locally for review.
    #
    # `approval_is_explicit` rather than truthiness, matching require_paid_gate
    # and gate_cleared: this is the second route to the paid video tier, and it
    # already exists because the first gate was attached to routes instead of to
    # the spend. Asking the same question the same way is the point -- two doors
    # onto the same money that disagree about what "approved" means is how the
    # next one gets missed.
    paid = [s.id for s in plan.coverage if s.motion_type == "ai_video"]
    if paid and not approval_is_explicit(getattr(sb, "storyboard_approved", False)):
        raise PlanError(
            f"{plan.beat_id}: {len(paid)} shot(s) want paid video ({paid}) but the "
            f"storyboard is not approved. Approve it first — that is where the "
            f"render budget is allocated. Static and parallax coverage can compile "
            f"before approval."
        )

    # Queue behind any other compile. See _COMPILE_LOCK.
    if not _COMPILE_LOCK.acquire(blocking=False):
        log("  another beat is compiling — waiting for it to finish...")
        _COMPILE_LOCK.acquire()
    try:
        return _compile_locked(plan, beat, sb, render_dir, backend, log, skip_existing)
    finally:
        _COMPILE_LOCK.release()


def _compile_locked(plan: CoveragePlan, beat: Shot, sb: Storyboard, render_dir: Path,
                    backend: str | None, log, skip_existing: bool) -> Path:
    out_dir = Path(render_dir) / plan.beat_id
    out_dir.mkdir(parents=True, exist_ok=True)

    plan.status = "compiling"
    save_plan(plan)

    img_backend = backend or getattr(sb.render, "backend", "") or "nano2"
    failures: list[str] = []
    # Shots that only finished because they were rendered twice. Reported in the
    # completion line, so a compile that needed a retry cannot be mistaken for
    # one that did not.
    retried: list[str] = []

    for i, ds in enumerate(plan.coverage, start=1):
        target = out_dir / f"{ds.id}.mp4"
        if skip_existing and ds.clip and target.is_file() and not ds.error:
            log(f"[{i}/{len(plan.coverage)}] {ds.id}: already rendered — skipping")
            continue

        log(f"[{i}/{len(plan.coverage)}] {ds.id} — {ds.shot_size or '?'} "
            f"{ds.purpose or ''} ({ds.duration:.2f}s)")
        ds.error = ""
        # A retry re-enters the whole attempt, so anything inside it that is not
        # idempotent needs saying once. The library branch's provenance row is
        # the only such thing: copying a file twice is harmless, writing the
        # ledger twice invents a second reuse of one asset. The generated branch
        # needs no equivalent because its own reuse guards already cover it --
        # ds.draft_variations is written before the render is attempted, so the
        # second attempt takes the reuse arm and buys nothing.
        recorded_library = False
        for attempt in range(1, LOCAL_RENDER_ATTEMPTS + 1):
            # Flipped to True at the exact point where everything still to run
            # for THIS shot is local and costs nothing -- and only there. While
            # it is False a failure may already have been billed, or may be
            # billed by the retry itself, and the shot is abandoned instead.
            # See LOCAL_RENDER_ATTEMPTS, and the two assignments below.
            local_only = False
            try:
                if ds.source == "library":
                    src = resolve_library_ref(ds.source_ref)
                    if not src:
                        raise PlanError(f"library asset not found: {ds.source_ref}")

                    # Reusing the beat's own existing clip is the natural way to keep
                    # motion that has already been paid for -- but this compile ends by
                    # writing that exact path, so the plan would cite a file it is about
                    # to destroy. It survives one run (the copy happens first) and then
                    # becomes unreproducible. Promote it to the series library instead,
                    # and rewrite the reference to the stable location.
                    beat_clip = Path(render_dir) / f"{plan.beat_id}.mp4"
                    if src.resolve() == beat_clip.resolve():
                        stable = library_root() / f"{plan.beat_id}__{ds.id}.mp4"
                        stable.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copyfile(src, stable)
                        log(f"  promoted {src.name} to the shot library as {stable.name} "
                            f"(it was the beat clip this compile overwrites)")
                        ds.source_ref = stable.name
                        src = stable
                    shutil.copyfile(Path(src), target)
                    log(f"  reused library asset {ds.source_ref}")
                    # Reused assets still need provenance, or the taste data grows a
                    # hole exactly where the cheapest shots are.
                    if not recorded_library:
                        ledger.record_generation(
                            scene_id=ds.id, path=config.rel_media_path(target),
                            strategy="library", prompt=ds.prompt or ds.source_ref,
                            backend="library", batch=plan.plan_id or plan.beat_id,
                            slot=i - 1,
                            style_medium=getattr(beat, "style_medium", "") or "",
                            motion_type=ds.motion_type,
                        )
                        recorded_library = True
                    # A library shot buys nothing at all, so the ffmpeg calls
                    # below it are retryable — the copy above is idempotent and
                    # the ledger row is guarded.
                    local_only = True
                else:
                    # lazy: only generated shots need these
                    from . import assets, capabilities, motion

                    synth = _synthetic_shot(ds, beat)

                    # A shot showing a known character gets their likeness, and their
                    # anchor text via Shot.references -> _character_clause.
                    who, ref_path = character_in_shot(ds)
                    subject_url = ""
                    if who:
                        synth.references = list(dict.fromkeys(list(synth.references) + [who]))
                        if ref_path:
                            import fal_client
                            subject_url = fal_client.upload_file(ref_path)
                            log(f"  {ds.id}: {who} likeness reference attached")
                        elif ds.face_visibility in ("moderate", "high"):
                            log(f"  !! {ds.id} shows {who} at face_visibility="
                                f"{ds.face_visibility} but {who} has no reference image — "
                                f"expect a different face. Upload one via "
                                f"POST /api/characters/{who}/reference")

                    if ds.draft_variations:
                        # Resumed run: reuse the stills already paid for.
                        synth.draft_variations = list(ds.draft_variations)
                        synth.chosen_variation = ds.chosen_variation
                        synth.draft_image = (ds.draft_variations[ds.chosen_variation or 0])
                    else:
                        # Stills bought during a compile are money, and this path
                        # recorded none of it: it called assets.generate_for_shot
                        # directly, so generation.spend() reported the video tier
                        # alone. The first real compile bought $1.20 of stills and the
                        # ledger's total for the beat was $1.20 — the clips only. A
                        # spend the ledger cannot see is a spend nobody can reconcile
                        # against a bill (§6.1), so it opens an attempt like every
                        # other charge.
                        #
                        # Recording, not gating: signature="" skips begin()'s reuse
                        # and in_flight arms, for the same reason main.py's draft path
                        # does — a deliberate re-draft is a legitimate re-buy. Opening
                        # it still fails closed, because "money spent, no record" is
                        # the whole defect.
                        n = still_takes(ds)
                        still_price = round(capabilities.COST_PER_IMAGE * n, 4)
                        att_img, how_img = generation.begin(
                            beat_id=plan.beat_id, shot_id=ds.id, signature="",
                            kind="image", backend=img_backend, paid=True,
                            estimated_cost=still_price)
                        if how_img != "created":
                            raise PlanError(
                                f"{ds.id}: the generation ledger answered {how_img!r} "
                                f"for its stills; refusing to generate. Resolve "
                                f"attempt {att_img.attempt} before compiling again.")
                        try:
                            assets.generate_for_shot(
                                synth, n, backend=img_backend, render=sb.render, log=log,
                                subject_url=subject_url or None)
                        except BaseException as exc:
                            # in_doubt, not fail: fal may already have been called and
                            # charged, and nothing here can tell. Same rule the video
                            # path below uses.
                            generation.in_doubt(plan.beat_id, att_img.id,
                                                f"still generation raised: {exc}")
                            raise
                        ds.draft_variations = list(synth.draft_variations)
                        ds.chosen_variation = synth.chosen_variation
                        # No cost=, for the reason given at the video settle below:
                        # fal reports no billed amount, so the figure stays in
                        # estimated_cost where it is labelled as ours.
                        generation.succeed(plan.beat_id, att_img.id,
                                           output=(ds.draft_variations or [""])[0])

                    if ds.motion_type == "ai_video":
                        # generate_paid_clip downloads to `target`. ds.clip was only
                        # assigned AFTER normalize/fit below, so a post-processing
                        # failure left a paid mp4 sitting on disk that the resume guard
                        # could not see (it tests ds.clip and not ds.error, and the
                        # handler sets ds.error) -- and the retry the error message
                        # asks for went straight back to fal. Every attempt bought the
                        # same clip again. Spike F hit exactly this: the generation
                        # succeeded and the compile failed after it.
                        #
                        # So: generate only when nothing is on disk, and record the
                        # paid bytes the instant they land, before anything that can
                        # fail runs against them.
                        want = paid_signature(ds)
                        have = (ds.paid_clip
                                and ds.paid_clip == config.rel_media_path(target)
                                and ds.paid_signature == want
                                and target.is_file() and target.stat().st_size > 0)
                        if have:
                            log(f"  {ds.id}: paid clip already downloaded — re-running "
                                f"post-processing only, not re-billing")
                        else:
                            # No idempotency key is minted here, deliberately. A key
                            # derived from these same inputs would be identical on a
                            # legitimate re-buy -- media truncated or deleted -- and
                            # would refuse to replace footage that is genuinely gone.
                            # A random key would be a NEW key after the crash it is
                            # meant to survive, so it would not dedupe either. The
                            # crash window is closed by the in_flight guard below
                            # instead, which asks the question that actually matters:
                            # is an attempt for these inputs already running?
                            #
                            # Every paid generation opens an attempt first. The
                            # attempt is what decides whether money may be spent:
                            # only a "created" disposition permits a call to fal, so
                            # a duplicate request or an unchanged shot cannot reach
                            # the API however it arrives here (§11.1). The lineage
                            # is the same record, so a failure below stays attached
                            # to the shot instead of vanishing (§11.6).
                            #
                            # Priced through paid_clip_price, which is the same
                            # function that produced the quote on the button. Before,
                            # this was a flat PAID_CLIP_COST while the quote came off
                            # the per-second table, and the two simply disagreed.
                            clip_price = paid_clip_price(ds)
                            if clip_price is None:
                                # generate_paid_clip would refuse below anyway, but
                                # the attempt is opened FIRST -- so refusing here is
                                # the difference between no record and a paid attempt
                                # carrying a confident $0.00 for a call that could
                                # never be made.
                                raise PlanError(unproducible(ds))
                            att, how = generation.begin(
                                beat_id=plan.beat_id, shot_id=ds.id, signature=want,
                                kind="video", backend=ds.backend, paid=True,
                                estimated_cost=clip_price,
                                exists=_media_present,
                            )
                            if how == "in_flight":
                                # The provider may already have been paid. Refusing
                                # is the only answer that cannot bill twice.
                                raise PlanError(
                                    f"{ds.id}: a paid generation for these inputs is "
                                    f"still recorded as running (attempt "
                                    f"{att.attempt}). It may have been billed. Resolve "
                                    f"it before retrying — see generation.abandon().")
                            if how != "created":
                                log(f"  {ds.id}: not re-billing — {how} "
                                    f"(attempt {att.attempt})")
                                ds.selected_attempt = att.id
                                ds.clip = att.output or ds.clip
                                ds.paid_clip = att.output or ds.paid_clip
                                ds.paid_signature = want
                                save_plan(plan)
                            else:
                                # --- before dispatch -------------------------------
                                # Everything here happens BEFORE the provider is
                                # called, so a failure in it carries no billing
                                # uncertainty and must close the attempt as an
                                # ordinary, retryable failure. Leaving it in doubt
                                # stranded the beat behind a recovery step that
                                # exists for a risk that had not been taken: a
                                # permission error deleting a stale file would
                                # record a possibly-billed attempt with no reason.
                                try:
                                    # Anything at `target` now is either a free
                                    # render or a clip bought for different inputs.
                                    # Neither is this shot's paid clip, so it must
                                    # not be mistaken for one.
                                    if target.is_file() and not ds.paid_clip:
                                        log(f"  {ds.id}: discarding a non-paid file "
                                            f"already at {target.name} before generating")
                                    target.unlink(missing_ok=True)
                                except BaseException as exc:
                                    generation.fail(plan.beat_id, att.id,
                                                    f"before dispatch: {exc}")
                                    raise
                                # --- dispatch --------------------------------------
                                billed: list = []
                                try:
                                    generate_paid_clip(ds, synth, sb, out_dir, log=log,
                                                       on_billed=billed.append)
                                except BaseException as exc:
                                    # NOT marked failed. Once the provider has been
                                    # called, whether it billed is unknown, and
                                    # "failed" invites a retry that buys the clip a
                                    # second time. It stays running so the next
                                    # request sees it as in_flight and refuses,
                                    # until a human resolves it.
                                    generation.in_doubt(plan.beat_id, att.id, str(exc))
                                    raise
                                ds.clip = config.rel_media_path(target)
                                ds.paid_clip = ds.clip
                                ds.paid_signature = want
                                ds.selected_attempt = att.id
                                # cost= is still left unset ON PURPOSE: a bare
                                # figure has no provenance, and writing our own
                                # into `cost` is what made every attempt read as a
                                # confirmed invoice.
                                #
                                # `measurement` is the one thing that may claim
                                # otherwise, and it is fal's own answer or nothing.
                                # An earlier version of this comment said fal does
                                # not report a billed amount at all -- that was
                                # wrong about the API, not just incomplete.
                                # fal_client.subscribe drops the response headers,
                                # and `x-fal-billable-units` is in them; the number
                                # was one free HTTP GET away the whole time.
                                # `billed` is empty whenever fal would not say, and
                                # then the attempt keeps its estimate, labelled as
                                # an estimate.
                                generation.succeed(plan.beat_id, att.id, ds.clip,
                                                   measurement=(billed or [None])[0])
                                save_plan(plan)
                    else:
                        # Tier A/B. Every charge this shot can incur is already
                        # behind us: the stills were either reused from a prior
                        # run or bought and settled in the ledger just above,
                        # and ds.draft_variations now holds them, so a second
                        # attempt takes the reuse branch and calls fal for
                        # nothing. What remains -- render_shot, normalize, fit
                        # -- is local, free, and repeatable.
                        #
                        # Note this deliberately does NOT cover the `ai_video`
                        # branch above, including the case where the clip is
                        # already bought and only post-processing failed. The
                        # money is spent either way there, but a retry has to
                        # re-enter the shot, and the guard that stops a second
                        # purchase (`have`) requires the downloaded file to
                        # still be non-empty -- while the operation that failed
                        # is `shutil.copyfile` over that very file. A kill in
                        # the middle of it leaves nothing to protect, and
                        # tests/test_paid_rebill.py says plainly what happens
                        # next to a paid clip whose bytes are gone: it is bought
                        # again, deliberately. A failure alone cannot tell a
                        # destroyed download from an intact one, so paid shots
                        # are not retried at all.
                        local_only = True
                        motion.render_shot(synth, out_dir=out_dir, storyboard=sb)

                normalize_clip(target, log=log)
                fit_clip(target, ds.duration, gestural=ds.gestural, log=log)
                ds.clip = config.rel_media_path(target)
            except Exception as exc:  # noqa: BLE001
                # One shot failing must not abandon the others already paid for.
                if local_only and attempt < LOCAL_RENDER_ATTEMPTS:
                    # Free tier only: nothing between here and the end of the
                    # shot can be charged for, so a second go costs wall clock
                    # and nothing else. Said out loud, because a silent retry
                    # that later succeeds would read as a clean first run.
                    log(f"  .. {ds.id} attempt {attempt} failed ({exc}) — "
                        f"retrying; this is a local render, nothing is re-bought")
                    continue
                ds.error = str(exc)
                failures.append(ds.id)
                if attempt > 1:
                    log(f"  !! {ds.id} FAILED after {attempt} attempts "
                        f"(retried, still failed): {exc}")
                else:
                    log(f"  !! {ds.id} FAILED: {exc}")
                break
            else:
                if attempt > 1:
                    retried.append(ds.id)
                    log(f"  {ds.id}: succeeded on attempt {attempt} "
                        f"(the first one failed and was retried)")
                break
        save_plan(plan)

    if failures:
        plan.status = "locked"
        save_plan(plan)
        raise PlanError(
            f"{plan.beat_id}: {len(failures)} shot(s) failed ({failures}) — the beat "
            f"clip was left untouched. Fix and re-run; finished shots are kept."
        )

    beat_clip = Path(render_dir) / f"{plan.beat_id}.mp4"
    concat([out_dir / f"{ds.id}.mp4" for ds in plan.coverage], beat_clip, log=log)

    # Belt and braces: concat rounding across seven clips can drift a frame or two.
    fit_clip(beat_clip, float(beat.camera.duration), gestural=False, log=log)

    final = probe_seconds(beat_clip)
    # A compile that needed a retry must not read like one that did not.
    retry_note = (f" — {len(retried)} shot(s) needed a retry: {retried}"
                  if retried else "")
    log(f"  {plan.beat_id}: {len(plan.coverage)} shots -> {final:.2f}s "
        f"(beat is {float(beat.camera.duration):.2f}s){retry_note}")

    plan.status = "compiled"
    plan.compiled = {
        "beat_clip": config.rel_media_path(beat_clip),
        "runtime": round(final, 3),
        "shots": len(plan.coverage),
        "sub_clips": [ds.clip for ds in plan.coverage],
        "retried": list(retried),
    }
    save_plan(plan)
    return beat_clip
