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
    (render_dir / "s011.01.mp4").write_bytes(b"")     # truncated download

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
