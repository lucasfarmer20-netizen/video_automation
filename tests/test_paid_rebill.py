"""A paid clip must be bought once, no matter how many times the compile fails.

CLAUDE.md targets $15-25 per finished episode. compile_coverage assigned ds.clip
only AFTER normalize_clip and fit_clip, so a post-processing failure left a paid
mp4 on disk that the resume guard could not see -- it tests `ds.clip and not
ds.error`, and the handler sets ds.error. The retry the error message asks the
operator to run went straight back to fal and bought the same clip again, without
bound. Spike F hit exactly this shape: the paid generation succeeded and the
compile failed after it.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
for m in ("anthropic", "fal_client", "elevenlabs"):
    sys.modules.setdefault(m, types.ModuleType(m))

from backend import director  # noqa: E402
from backend.manifest import Camera, Shot, Storyboard  # noqa: E402


@pytest.fixture
def scene(tmp_path, monkeypatch):
    monkeypatch.setattr(director.config, "MANIFEST_PATH",
                        tmp_path / "storyboard_manifest.json")
    render_dir = tmp_path / "render"
    render_dir.mkdir()

    plan = director.CoveragePlan(beat_id="s011", beat_duration=5.0, status="locked")
    ds = director.DirectorShot(id="s011.01", beat_id="s011", motion_type="ai_video",
                               camera=Camera(move="static", duration=5.0),
                               prompt="a hand on drill steel", subject="single jack",
                               shot_size="m", purpose="master")
    ds.draft_variations = ["a.png"]
    ds.chosen_variation = 0
    plan.coverage = [ds]
    director.save_plan(plan)

    sb = Storyboard(title="T", storyboard_approved=True,
                    shots=[Shot(scene_id="s011", narration="n", prompt="p",
                                camera=Camera(move="static", duration=5.0))])
    return plan, sb, render_dir


def test_a_failing_compile_does_not_re_bill_the_paid_clip(scene, monkeypatch):
    plan, sb, render_dir = scene
    calls = {"paid": 0}

    def fake_paid(ds, synth, sb_, out_dir, log=print):
        calls["paid"] += 1
        target = Path(out_dir) / f"{ds.id}.mp4"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"PAID-BYTES")     # the clip fal charged for
        return target

    def boom(*a, **k):
        raise RuntimeError("ffmpeg: moov atom not found")

    monkeypatch.setattr(director, "generate_paid_clip", fake_paid)
    monkeypatch.setattr(director, "normalize_clip", boom)   # fails AFTER the spend
    monkeypatch.setattr(director, "concat", lambda *a, **k: Path("beat.mp4"))

    for attempt in range(3):
        with pytest.raises(director.PlanError):
            director.compile_coverage(director.load_plan("s011"), sb, render_dir,
                                      log=lambda m: None)

    assert calls["paid"] == 1, (
        f"fal was called {calls['paid']} times for one shot — every retry of a "
        f"failed compile re-bought the clip")


def test_the_paid_clip_is_recorded_before_anything_that_can_fail(scene, monkeypatch):
    """The record and the bytes must not be able to diverge."""
    plan, sb, render_dir = scene

    def fake_paid(ds, synth, sb_, out_dir, log=print):
        (Path(out_dir) / f"{ds.id}.mp4").write_bytes(b"PAID-BYTES")

    monkeypatch.setattr(director, "generate_paid_clip", fake_paid)
    monkeypatch.setattr(director, "normalize_clip",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    monkeypatch.setattr(director, "concat", lambda *a, **k: Path("beat.mp4"))

    with pytest.raises(director.PlanError):
        director.compile_coverage(director.load_plan("s011"), sb, render_dir,
                                  log=lambda m: None)

    saved = director.load_plan("s011").coverage[0]
    assert saved.clip, "the paid clip must be recorded even though the compile failed"
    assert saved.error, "and the failure must still be reported"


def test_a_zero_byte_download_is_not_mistaken_for_a_paid_clip(scene, monkeypatch):
    """An empty file must not make the guard skip a generation that never landed."""
    plan, sb, render_dir = scene
    calls = {"paid": 0}
    shot_dir = render_dir / "s011"
    shot_dir.mkdir(parents=True, exist_ok=True)
    (shot_dir / "s011.01.mp4").write_bytes(b"")   # truncated download

    def fake_paid(ds, synth, sb_, out_dir, log=print):
        calls["paid"] += 1
        (Path(out_dir) / f"{ds.id}.mp4").write_bytes(b"PAID-BYTES")

    monkeypatch.setattr(director, "generate_paid_clip", fake_paid)
    monkeypatch.setattr(director, "normalize_clip", lambda *a, **k: None)
    monkeypatch.setattr(director, "fit_clip", lambda *a, **k: None)
    monkeypatch.setattr(director, "concat", lambda *a, **k: Path("beat.mp4"))

    director.compile_coverage(director.load_plan("s011"), sb, render_dir,
                              log=lambda m: None)
    assert calls["paid"] == 1, "a zero-byte file must not count as an existing clip"


def test_a_free_render_left_at_the_target_is_not_mistaken_for_paid(scene, monkeypatch):
    """motion.render_shot writes the SAME path as a paid download, and out_dir is
    render/<beat_id>/ -- stable across re-plans, never cleared. A guard that only
    asked "is there a file at target?" accepted a free parallax render of a shot
    later promoted to ai_video as though fal had produced it: no generation, no
    charge, and the wrong footage shipped as the paid clip."""
    plan, sb, render_dir = scene
    calls = {"paid": 0}
    shot_dir = render_dir / "s011"
    shot_dir.mkdir(parents=True, exist_ok=True)
    (shot_dir / "s011.01.mp4").write_bytes(b"FREE-PARALLAX-RENDER")

    def fake_paid(ds, synth, sb_, out_dir, log=print):
        calls["paid"] += 1
        (Path(out_dir) / f"{ds.id}.mp4").write_bytes(b"PAID-BYTES")

    monkeypatch.setattr(director, "generate_paid_clip", fake_paid)
    monkeypatch.setattr(director, "normalize_clip", lambda *a, **k: None)
    monkeypatch.setattr(director, "fit_clip", lambda *a, **k: None)
    monkeypatch.setattr(director, "concat", lambda *a, **k: Path("beat.mp4"))

    director.compile_coverage(director.load_plan("s011"), sb, render_dir,
                              log=lambda m: None)
    assert calls["paid"] == 1, "a free render must not count as the paid clip"
    assert (shot_dir / "s011.01.mp4").read_bytes() == b"PAID-BYTES"


def test_a_clip_bought_for_different_inputs_is_not_reused(scene, monkeypatch):
    """A stale mp4 from a discarded plan sits at exactly the same path."""
    plan, sb, render_dir = scene
    calls = {"paid": 0}

    def fake_paid(ds, synth, sb_, out_dir, log=print):
        calls["paid"] += 1
        (Path(out_dir) / f"{ds.id}.mp4").write_bytes(b"PAID-BYTES")

    monkeypatch.setattr(director, "generate_paid_clip", fake_paid)
    monkeypatch.setattr(director, "normalize_clip", lambda *a, **k: None)
    monkeypatch.setattr(director, "fit_clip", lambda *a, **k: None)
    monkeypatch.setattr(director, "concat", lambda *a, **k: Path("beat.mp4"))

    director.compile_coverage(director.load_plan("s011"), sb, render_dir,
                              log=lambda m: None)
    assert calls["paid"] == 1

    # Same beat, same shot id, different content: a new prompt must re-generate.
    p2 = director.load_plan("s011")
    p2.coverage[0].motion_prompt = "a completely different move"
    director.save_plan(p2)
    director.compile_coverage(director.load_plan("s011"), sb, render_dir,
                              log=lambda m: None, skip_existing=False)
    assert calls["paid"] == 2, "changing the prompt must not reuse the old paid clip"


def test_an_unchanged_shot_is_still_never_re_billed(scene, monkeypatch):
    """The point of the guard: identical inputs, no second charge."""
    plan, sb, render_dir = scene
    calls = {"paid": 0}

    def fake_paid(ds, synth, sb_, out_dir, log=print):
        calls["paid"] += 1
        (Path(out_dir) / f"{ds.id}.mp4").write_bytes(b"PAID-BYTES")

    monkeypatch.setattr(director, "generate_paid_clip", fake_paid)
    monkeypatch.setattr(director, "fit_clip", lambda *a, **k: None)
    monkeypatch.setattr(director, "concat", lambda *a, **k: Path("beat.mp4"))
    # Fails after the spend, twice, then succeeds.
    state = {"n": 0}
    def flaky(*a, **k):
        state["n"] += 1
        if state["n"] <= 2:
            raise RuntimeError("ffmpeg: moov atom not found")
    monkeypatch.setattr(director, "normalize_clip", flaky)

    for _ in range(2):
        with pytest.raises(director.PlanError):
            director.compile_coverage(director.load_plan("s011"), sb, render_dir,
                                      log=lambda m: None)
    director.compile_coverage(director.load_plan("s011"), sb, render_dir,
                              log=lambda m: None)
    assert calls["paid"] == 1, f"fal charged {calls['paid']} times for one clip"


def test_a_recorded_paid_clip_that_got_truncated_is_regenerated(scene, monkeypatch):
    """The size clause, which only bites once paid_clip and the signature already
    match: the record says we bought it and the bytes are gone. Skipping there
    would ship an empty file as the paid clip and never recover without a manual
    delete -- the same permanence as the poison narration file."""
    plan, sb, render_dir = scene
    calls = {"paid": 0}

    def fake_paid(ds, synth, sb_, out_dir, log=print):
        calls["paid"] += 1
        (Path(out_dir) / f"{ds.id}.mp4").write_bytes(b"PAID-BYTES")

    monkeypatch.setattr(director, "generate_paid_clip", fake_paid)
    monkeypatch.setattr(director, "normalize_clip", lambda *a, **k: None)
    monkeypatch.setattr(director, "fit_clip", lambda *a, **k: None)
    monkeypatch.setattr(director, "concat", lambda *a, **k: Path("beat.mp4"))

    director.compile_coverage(director.load_plan("s011"), sb, render_dir,
                              log=lambda m: None)
    assert calls["paid"] == 1
    saved = director.load_plan("s011").coverage[0]
    assert saved.paid_clip and saved.paid_signature

    # The record survives; the bytes do not.
    (render_dir / "s011" / "s011.01.mp4").write_bytes(b"")
    director.compile_coverage(director.load_plan("s011"), sb, render_dir,
                              log=lambda m: None, skip_existing=False)

    assert calls["paid"] == 2, "a truncated paid clip must be re-fetched, not shipped"
    assert (render_dir / "s011" / "s011.01.mp4").read_bytes() == b"PAID-BYTES"


def test_the_signature_is_what_makes_the_marker_meaningful(scene):
    """paid_clip alone cannot distinguish this shot's clip from a stale one at the
    same path -- the signature is the clause carrying the guarantee. Recorded so
    the redundancy between the two is deliberate rather than accidental."""
    ds = director.load_plan("s011").coverage[0]
    before = director.paid_signature(ds)
    ds.camera.duration = ds.duration + 3.0
    assert director.paid_signature(ds) != before, "length must change the signature"
    ds.camera.duration -= 3.0
    ds.motion_prompt = "an entirely different move"
    assert director.paid_signature(ds) != before, "prompt must change the signature"
