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
import shutil
import subprocess
import threading
from dataclasses import asdict, dataclass, field
from pathlib import Path

from . import config, ledger
from .manifest import Camera, MotionType, Shot, Storyboard

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
    warnings: list[str] = field(default_factory=list)
    compiled: dict = field(default_factory=dict)
    # The scene's stated approach and its physical layout. Held per beat because
    # beats are the unit of storage, even though both are decided per scene.
    visual_strategy: str = ""
    blocking: dict = field(default_factory=dict)

    def total_duration(self) -> float:
        return sum(s.duration for s in self.coverage)



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

def director_dir() -> Path:
    return config.MANIFEST_PATH.parent / "director"


def plan_path(beat_id: str) -> Path:
    return director_dir() / f"{beat_id}.json"


def load_plan(beat_id: str) -> CoveragePlan | None:
    p = plan_path(beat_id)
    if not p.is_file():
        return None
    raw = json.loads(p.read_text(encoding="utf-8"))
    shots = []
    for d in raw.get("coverage") or []:
        cam = Camera(**(d.pop("camera", None) or {}))
        d.pop("duration", None)  # tolerate hand-authored files that add it
        shots.append(DirectorShot(camera=cam, **d))
    raw["coverage"] = shots
    raw.pop("version", None)
    return CoveragePlan(version=PLAN_VERSION, **raw)


def save_plan(plan: CoveragePlan) -> Path:
    d = director_dir()
    d.mkdir(parents=True, exist_ok=True)
    p = plan_path(plan.beat_id)
    p.write_text(json.dumps(asdict(plan), indent=2), encoding="utf-8")
    return p


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
    return shutil.which("ffmpeg") or "ffmpeg"


def _ffprobe() -> str:
    return shutil.which("ffprobe") or "ffprobe"


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

    if gestural:
        raise PlanError(
            f"{path.name}: is {have:.2f}s but the shot wants {want:.2f}s, and it is "
            f"marked gestural so it must not be trimmed mid-movement — regenerate it "
            f"at a legal duration for this model, or clear `gestural`"
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


def generate_paid_clip(ds: DirectorShot, synth: Shot, sb: Storyboard,
                       out_dir: Path, log=print) -> Path:
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
    """
    import fal_client

    from . import assets

    from . import capabilities

    want = float(ds.duration)

    # Go through the router rather than trusting the backend recorded at plan
    # time. This function previously resolved the model itself and applied its own
    # max(3, min(10, ...)) -- the exact hardcoded clamp Spike C exists to replace --
    # so the capability table was consulted when PLANNING and ignored when
    # GENERATING. That is how a 3.34s gestural shot asked wan_2_7 for 3s, received
    # a fixed 5s clip, and then could not be trimmed.
    routed = capabilities.resolve(
        {"duration": want, "gestural": ds.gestural},
        prefer=[ds.backend] if ds.backend else None)
    if not routed.get("backend") and ds.backend:
        # The recorded model cannot serve this shot any more -- usually because a
        # capability entry was corrected after the plan was written. Re-resolve
        # across everything rather than failing on a stale choice.
        routed = capabilities.resolve({"duration": want, "gestural": ds.gestural})
    if not routed.get("backend"):
        raise PlanError(
            f"{ds.id}: no configured model can produce {want:.2f}s"
            + (" without trimming a gesture" if ds.gestural else "")
            + f". {routed.get('reason', '')}")

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

    result = fal_client.subscribe(endpoint, arguments=arguments, with_logs=False)
    url = (result.get("video") or {}).get("url") or (result.get("file") or {}).get("url")
    if not url:
        raise PlanError(f"{ds.id}: no video URL returned by {endpoint}")

    dest = out_dir / f"{ds.id}.mp4"
    assets._download(url, dest)
    log(f"  downloaded {dest.name}")
    return dest


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


def compile_coverage(plan: CoveragePlan, sb: Storyboard, render_dir: Path,
                     backend: str | None = None, log=print,
                     skip_existing: bool = True) -> Path:
    """Render every Director Shot and assemble the beat clip.

    Resume-safe by the same rule as the rest of the pipeline: a shot that already
    has a current clip is skipped, so re-running after a failure costs only what
    failed. The plan is saved after each shot for the same reason.
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
    paid = [s.id for s in plan.coverage if s.motion_type == "ai_video"]
    if paid and not getattr(sb, "storyboard_approved", False):
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

    for i, ds in enumerate(plan.coverage, start=1):
        target = out_dir / f"{ds.id}.mp4"
        if skip_existing and ds.clip and target.is_file() and not ds.error:
            log(f"[{i}/{len(plan.coverage)}] {ds.id}: already rendered — skipping")
            continue

        log(f"[{i}/{len(plan.coverage)}] {ds.id} — {ds.shot_size or '?'} "
            f"{ds.purpose or ''} ({ds.duration:.2f}s)")
        ds.error = ""
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
                ledger.record_generation(
                    scene_id=ds.id, path=config.rel_media_path(target),
                    strategy="library", prompt=ds.prompt or ds.source_ref,
                    backend="library", batch=plan.plan_id or plan.beat_id, slot=i - 1,
                    style_medium=getattr(beat, "style_medium", "") or "",
                    motion_type=ds.motion_type,
                )
            else:
                from . import assets, motion   # lazy: only generated shots need these

                synth = _synthetic_shot(ds, beat)
                if ds.draft_variations:
                    # Resumed run: reuse the stills already paid for.
                    synth.draft_variations = list(ds.draft_variations)
                    synth.chosen_variation = ds.chosen_variation
                    synth.draft_image = (ds.draft_variations[ds.chosen_variation or 0])
                else:
                    n = 4 if ds.identity_critical else 1
                    assets.generate_for_shot(
                        synth, n, backend=img_backend, render=sb.render, log=log)
                    ds.draft_variations = list(synth.draft_variations)
                    ds.chosen_variation = synth.chosen_variation
                    ds.estimated_cost += n * 0.15

                if ds.motion_type == "ai_video":
                    generate_paid_clip(ds, synth, sb, out_dir, log=log)
                    ds.estimated_cost += 0.60      # rough; real figure comes from fal
                else:
                    motion.render_shot(synth, out_dir=out_dir, storyboard=sb)

            normalize_clip(target, log=log)
            fit_clip(target, ds.duration, gestural=ds.gestural, log=log)
            ds.clip = config.rel_media_path(target)
        except Exception as exc:  # noqa: BLE001
            # One shot failing must not abandon the others already paid for.
            ds.error = str(exc)
            failures.append(ds.id)
            log(f"  !! {ds.id} FAILED: {exc}")
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
    log(f"  {plan.beat_id}: {len(plan.coverage)} shots -> {final:.2f}s "
        f"(beat is {float(beat.camera.duration):.2f}s)")

    plan.status = "compiled"
    plan.compiled = {
        "beat_clip": config.rel_media_path(beat_clip),
        "runtime": round(final, 3),
        "shots": len(plan.coverage),
        "sub_clips": [ds.clip for ds in plan.coverage],
    }
    save_plan(plan)
    return beat_clip
