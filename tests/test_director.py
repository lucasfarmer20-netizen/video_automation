"""Spike B validation tests — the checks that must hold before money is spent.

Every test here runs offline. The ones that need ffmpeg skip cleanly when it is
absent, so this file is useful on a workstation without the media toolchain and
complete on the server that has it.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend import director  # noqa: E402
from backend.director import CoveragePlan, DirectorShot, PlanError  # noqa: E402
from backend.manifest import Camera, Shot  # noqa: E402

HAS_FFMPEG = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None
needs_ffmpeg = pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg/ffprobe not installed")


def _beat(seconds: float = 27.0) -> Shot:
    return Shot(scene_id="s003", narration="x", camera=Camera(duration=seconds))


def _plan(durations, beat_seconds=27.0, **kw) -> CoveragePlan:
    return CoveragePlan(
        beat_id="s003",
        beat_duration=beat_seconds,
        status=kw.pop("status", "locked"),
        coverage=[
            DirectorShot(id=f"s003.{i+1:02d}", beat_id="s003", prompt="a thing",
                         camera=Camera(duration=d), **kw)
            for i, d in enumerate(durations)
        ],
    )


# --- validation: refuse before spending ---------------------------------------

def test_coverage_must_fill_the_beat():
    """A plan that does not add up must fail before a single image is generated."""
    plan = _plan([3.0, 4.0, 5.0])          # 12s of coverage
    with pytest.raises(PlanError, match="coverage must fill the beat"):
        director.validate(plan, _beat(27.0))


def test_coverage_that_fills_the_beat_passes():
    plan = _plan([10.0, 10.0, 7.0])
    director.validate(plan, _beat(27.0))   # must not raise


def test_stale_plan_is_rejected():
    """Narration re-recorded => beat duration moved => the plan is stale.

    This is the check that a 590.6s preview against a 672.0s manifest did not
    have, and reported 'complete' for its absence.
    """
    plan = _plan([10.0, 10.0, 7.0], beat_seconds=27.0)
    with pytest.raises(PlanError, match="replan"):
        director.validate(plan, _beat(31.5))


def test_duplicate_shot_ids_rejected():
    plan = _plan([13.5, 13.5])
    plan.coverage[1].id = plan.coverage[0].id
    with pytest.raises(PlanError, match="duplicate"):
        director.validate(plan, _beat(27.0))


def test_zero_duration_rejected():
    plan = _plan([27.0, 0.0])
    plan.beat_duration = 27.0
    with pytest.raises(PlanError):
        director.validate(plan, _beat(27.0))


def test_library_shot_needs_a_ref():
    plan = _plan([27.0])
    plan.coverage[0].source = "library"
    with pytest.raises(PlanError, match="source_ref"):
        director.validate(plan, _beat(27.0))


def test_empty_plan_rejected():
    plan = CoveragePlan(beat_id="s003", beat_duration=27.0, status="locked")
    with pytest.raises(PlanError, match="no coverage"):
        director.validate(plan, _beat(27.0))


# --- persistence ---------------------------------------------------------------

def test_plan_round_trips(tmp_path, monkeypatch):
    """Every field must survive JSON, or compile silently loses intent."""
    monkeypatch.setattr(director.config, "MANIFEST_PATH", tmp_path / "storyboard_manifest.json")
    plan = _plan([13.5, 13.5])
    plan.coverage[0].gestural = True
    plan.coverage[0].identity_critical = True
    plan.coverage[0].constrained_by = ["identity_reliability"]
    plan.coverage[1].source = "library"
    plan.coverage[1].source_ref = "render/s001/s001.02.mp4"
    plan.coverage[1].library_scope = "series"
    director.save_plan(plan)

    back = director.load_plan("s003")
    assert back is not None
    assert [s.id for s in back.coverage] == [s.id for s in plan.coverage]
    assert back.coverage[0].gestural is True
    assert back.coverage[0].identity_critical is True
    assert back.coverage[0].constrained_by == ["identity_reliability"]
    assert back.coverage[1].source_ref == "render/s001/s001.02.mp4"
    assert back.coverage[1].library_scope == "series"
    assert back.coverage[0].camera.duration == 13.5


def test_has_locked_coverage(tmp_path, monkeypatch):
    """The authority guard's predicate: draft must not lock out a normal render."""
    monkeypatch.setattr(director.config, "MANIFEST_PATH", tmp_path / "storyboard_manifest.json")
    assert director.has_locked_coverage("s003") is False

    plan = _plan([27.0], status="draft")
    director.save_plan(plan)
    assert director.has_locked_coverage("s003") is False

    plan.status = "locked"
    director.save_plan(plan)
    assert director.has_locked_coverage("s003") is True


# --- media: the concat contract ------------------------------------------------

def _make_clip(path: Path, seconds: float, height: int = 720, fps: int = 24,
               color: str = "black") -> Path:
    # libx264 needs even dimensions; 480*16/9 is 853, which it refuses.
    width = (height * 16 // 9) // 2 * 2
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-f", "lavfi",
         "-i", f"color=c={color}:s={width}x{height}:r={fps}",
         "-t", f"{seconds}", "-c:v", "libx264", "-crf", "20",
         "-pix_fmt", "yuv420p", str(path)],
        check=True,
    )
    return path


@needs_ffmpeg
def test_concat_totals_the_parts(tmp_path):
    clips = [_make_clip(tmp_path / f"c{i}.mp4", 1.0) for i in range(7)]
    out = director.concat(clips, tmp_path / "beat.mp4", log=lambda m: None)
    assert abs(director.probe_seconds(out) - 7.0) < 0.05


@needs_ffmpeg
def test_concat_refuses_missing_clips(tmp_path):
    clips = [_make_clip(tmp_path / "a.mp4", 1.0), tmp_path / "missing.mp4"]
    with pytest.raises(PlanError, match="missing clips"):
        director.concat(clips, tmp_path / "beat.mp4", log=lambda m: None)


@needs_ffmpeg
def test_concat_failure_leaves_previous_beat_intact(tmp_path):
    dest = _make_clip(tmp_path / "beat.mp4", 3.0)
    before = dest.read_bytes()
    with pytest.raises(PlanError):
        director.concat([tmp_path / "nope.mp4"], dest, log=lambda m: None)
    assert dest.read_bytes() == before


@needs_ffmpeg
def test_normalize_conforms_off_spec_media(tmp_path):
    """The §0.1 requirement: a 480p/30fps clip must become canonical.

    This is what padding was doing by accident and will stop doing once
    sub-clips match their shot durations exactly.
    """
    odd = _make_clip(tmp_path / "odd.mp4", 2.0, height=480, fps=30)
    assert director.is_canonical(odd) is False
    director.normalize_clip(odd, log=lambda m: None)
    assert director.is_canonical(odd) is True
    s = director.probe_stream(odd)
    assert s["height"] == director.CANON_HEIGHT
    assert abs(s["fps"] - director.CANON_FPS) < 0.01


@needs_ffmpeg
def test_normalize_forces_exact_canvas_not_just_height(tmp_path):
    """A non-16:9 source must land on exactly 1280x720, not 1278x720.

    `scale=-2:720` preserves aspect ratio, so 852x480 (1.775, not 1.7778) becomes
    1278 wide. Mixed widths break `-c copy` exactly as mixed heights do. The first
    dry run produced a beat clip containing both 1278 and 1280.
    """
    odd = _make_clip(tmp_path / "odd.mp4", 1.0, height=480, fps=30)
    director.normalize_clip(odd, log=lambda m: None)
    s = director.probe_stream(odd)
    assert s["width"] == director.CANON_WIDTH
    assert s["height"] == director.CANON_HEIGHT


@needs_ffmpeg
def test_concat_of_many_sources_is_dimensionally_uniform(tmp_path):
    """The end-to-end property: one resolution throughout the assembled beat."""
    clips = []
    for i, (h, fps) in enumerate([(720, 24), (480, 30), (1080, 25), (720, 24)]):
        c = _make_clip(tmp_path / f"m{i}.mp4", 1.0, height=h, fps=fps)
        director.normalize_clip(c, log=lambda m: None)
        clips.append(c)
    out = director.concat(clips, tmp_path / "beat.mp4", log=lambda m: None)
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "frame=width,height", "-of", "csv=p=0", str(out)],
        capture_output=True, text=True,
    )
    sizes = {ln.rstrip(",") for ln in probe.stdout.split() if ln.strip()}
    assert sizes == {f"{director.CANON_WIDTH},{director.CANON_HEIGHT}"}


@needs_ffmpeg
def test_heterogeneous_concat_works_after_normalization(tmp_path):
    """Mixed sources must survive `-c copy` once normalized — not before.

    Stream copy demands identical parameters. This is the failure a paid clip
    matching its shot duration would otherwise walk into.
    """
    a = _make_clip(tmp_path / "a.mp4", 1.0, height=720, fps=24)
    b = _make_clip(tmp_path / "b.mp4", 1.0, height=480, fps=30, color="red")
    director.normalize_clip(b, log=lambda m: None)
    out = director.concat([a, b], tmp_path / "mixed.mp4", log=lambda m: None)
    assert abs(director.probe_seconds(out) - 2.0) < 0.08


@needs_ffmpeg
def test_fit_pads_short_clips(tmp_path):
    c = _make_clip(tmp_path / "s.mp4", 2.0)
    director.fit_clip(c, 3.0, gestural=False, log=lambda m: None)
    assert abs(director.probe_seconds(c) - 3.0) < 0.06


@needs_ffmpeg
def test_fit_trims_ambient_motion(tmp_path):
    c = _make_clip(tmp_path / "l.mp4", 4.0)
    director.fit_clip(c, 3.2, gestural=False, log=lambda m: None)
    assert abs(director.probe_seconds(c) - 3.2) < 0.06


@needs_ffmpeg
def test_fit_refuses_to_trim_a_gesture(tmp_path):
    """Cutting a designed movement at 80% stops the head mid-turn."""
    c = _make_clip(tmp_path / "g.mp4", 4.0)
    with pytest.raises(PlanError, match="gestural"):
        director.fit_clip(c, 3.2, gestural=True, log=lambda m: None)
    assert abs(director.probe_seconds(c) - 4.0) < 0.06


# --- Gate 1: coverage must not be a back door to the paid tier ------------------

def _sb(approved: bool):
    from backend.manifest import Storyboard, RenderConfig
    return Storyboard(id="p", title="T", shots=[_beat(27.0)],
                      render=RenderConfig(), storyboard_approved=approved)


def test_paid_coverage_refused_until_the_storyboard_is_approved(tmp_path, monkeypatch):
    """Coverage is a second route to Tier C and must honour the same gate."""
    monkeypatch.setattr(director.config, "MANIFEST_PATH", tmp_path / "m.json")
    plan = _plan([27.0])
    plan.coverage[0].motion_type = "ai_video"
    director.save_plan(plan)
    with pytest.raises(PlanError, match="not approved"):
        director.compile_coverage(plan, _sb(approved=False), tmp_path / "render",
                                  log=lambda m: None)


def test_free_coverage_compiles_before_approval(tmp_path, monkeypatch):
    """Static and parallax cost nothing, so they stay open pre-gate."""
    monkeypatch.setattr(director.config, "MANIFEST_PATH", tmp_path / "m.json")
    plan = _plan([27.0])
    plan.coverage[0].motion_type = "parallax"
    director.save_plan(plan)
    # Fails later for want of media, but NOT on the approval check.
    with pytest.raises(Exception) as exc:
        director.compile_coverage(plan, _sb(approved=False), tmp_path / "render",
                                  log=lambda m: None)
    assert "not approved" not in str(exc.value)


@needs_ffmpeg
def test_gestural_tolerates_a_sub_frame_overrun(tmp_path):
    """A model asked for 5s returns 5.04s. That is a rounding error, not a gesture.

    The guard fired on 0.04s -- one frame at 24fps -- and failed a compile whose
    paid generation had otherwise succeeded end to end.
    """
    c = _make_clip(tmp_path / "g.mp4", 5.04)
    director.fit_clip(c, 5.00, gestural=True, log=lambda m: None)
    assert abs(director.probe_seconds(c) - 5.00) < 0.06


@needs_ffmpeg
def test_gestural_still_refuses_a_real_trim(tmp_path):
    """The guard must still stop a cut that could land mid-movement."""
    c = _make_clip(tmp_path / "g2.mp4", 5.0)
    with pytest.raises(PlanError, match="gestural"):
        director.fit_clip(c, 3.34, gestural=True, log=lambda m: None)
