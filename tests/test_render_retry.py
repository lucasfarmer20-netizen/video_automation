"""One transient ffmpeg death must not cost a human a whole beat — and must
never cost them a second charge.

Two compiles in twenty-three renders died the same way:

    !! s003.06 FAILED: [Errno 32] Broken pipe      (shot 6 of 7)
    !! s005.05 FAILED: [Errno 32] Broken pipe      (shot 5 of 6)

Both free-tier `parallax`, both the penultimate shot of their beat, both with an
empty `FFMPEG STDERR OUTPUT`. Six shots' worth of finished rendering ended in a
`PlanError` because the seventh hiccuped, and both times the human re-ran the
compile by hand and the same shot rendered immediately with no code change.

So a LOCAL render is attempted twice. The dangerous half of that sentence is
"local": a retry of a paid generation can buy the same clip a second time, and
that is the accepted-risk territory of issue #17, which must not widen silently.
The paid tests come FIRST in this file, and inside each of them the assertion
about money comes before every other assertion, because that is the one that
must never pass by accident.
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

BEAT = "s003"


def _scene(tmp_path, monkeypatch, *, motion_types, stills_on_disk=True,
           seconds=5.0):
    """A beat of `motion_types`, a faked provider, and a real ledger on disk.

    Nothing here reaches fal or ffmpeg. `probe_seconds` is stubbed with the rest:
    it is the tail-end log line that `backend/ffmpeg_bin.py` records as having
    masked a set of paid-render assertions once already, and none of these tests
    assert anything about a duration.
    """
    monkeypatch.setattr(director.config, "MANIFEST_PATH",
                        tmp_path / "storyboard_manifest.json")
    render_dir = tmp_path / "render"
    render_dir.mkdir(parents=True, exist_ok=True)

    plan = director.CoveragePlan(beat_id=BEAT, beat_duration=seconds,
                                 status="locked")
    each = seconds / len(motion_types)      # coverage must fill the beat exactly
    for n, mt in enumerate(motion_types, start=1):
        ds = director.DirectorShot(
            id=f"{BEAT}.{n:02d}", beat_id=BEAT, motion_type=mt,
            camera=Camera(move="static", duration=each),
            prompt="a hand on drill steel", subject="single jack",
            shot_size="m", purpose="master")
        if stills_on_disk:
            ds.draft_variations = [f"{ds.id}_v0.png"]
            ds.chosen_variation = 0
        plan.coverage.append(ds)
    director.save_plan(plan)

    sb = Storyboard(title="T", storyboard_approved=True,
                    shots=[Shot(scene_id=BEAT, narration="n", prompt="p",
                                camera=Camera(move="static", duration=seconds))])

    monkeypatch.setattr(director, "concat", lambda *a, **k: Path("beat.mp4"))
    monkeypatch.setattr(director, "probe_seconds", lambda *a, **k: seconds)
    return plan, sb, render_dir


def _log():
    """A log sink that keeps the lines, because the lines are the deliverable."""
    lines: list[str] = []
    return lines, lines.append


def _compile(sb, render_dir, log):
    return director.compile_coverage(director.load_plan(BEAT), sb, render_dir,
                                     log=log)


# --- money first ----------------------------------------------------------------

def test_a_paid_shot_that_fails_after_its_clip_is_bought_is_never_retried(
        tmp_path, monkeypatch):
    """The constraint that is not negotiable.

    The failure here is post-purchase: fal was called, the clip landed, and
    `normalize_clip` then died — and died the way the observed failures died,
    with the target file gone. That last detail is the whole argument. The guard
    that stops a second purchase (`have` in `_compile_locked`, and
    `generation.begin`'s `exists=`) both require the downloaded bytes to still be
    there, while the operation that failed is `shutil.copyfile` over exactly
    those bytes. `tests/test_paid_rebill.py::
    test_a_recorded_paid_clip_that_got_truncated_is_regenerated` says what
    happens next in as many words: the clip is bought again.

    So the money is "already spent either way" only until the retry, at which
    point it can be spent twice. A failure alone cannot distinguish an intact
    download from a destroyed one, so a paid shot is not retried at all.
    """
    plan, sb, render_dir = _scene(tmp_path, monkeypatch,
                                  motion_types=["ai_video"])
    calls = {"paid": 0, "normalize": 0}

    def fake_paid(ds, synth, sb_, out_dir, log=print):
        calls["paid"] += 1
        target = Path(out_dir) / f"{ds.id}.mp4"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"PAID-BYTES")
        return target

    def killed_normalize(path, log=print):
        calls["normalize"] += 1
        # A copyfile that dies partway through leaves nothing to protect.
        Path(path).unlink(missing_ok=True)
        raise BrokenPipeError(32, "Broken pipe")

    monkeypatch.setattr(director, "generate_paid_clip", fake_paid)
    monkeypatch.setattr(director, "normalize_clip", killed_normalize)
    monkeypatch.setattr(director, "fit_clip", lambda *a, **k: None)

    lines, log = _log()
    with pytest.raises(director.PlanError):
        _compile(sb, render_dir, log)

    assert calls["paid"] == 1, (
        f"fal was called {calls['paid']} times for one shot — a retry bought "
        f"the clip again")
    assert calls["normalize"] == 1, (
        "the paid shot was attempted twice; a paid shot must never be retried")
    assert not [ln for ln in lines if "retrying" in ln], (
        f"a paid shot was announced as retryable: {lines}")


def test_a_paid_shot_that_fails_during_generation_is_never_retried(
        tmp_path, monkeypatch):
    """The other half: the provider was called and nothing came back.

    Whether it billed is unknown, which is why `generation.in_doubt` leaves the
    attempt running and the next request sees `in_flight`. That ledger arm is a
    second line of defence and would refuse a re-dispatch anyway — so the
    assertion that actually discriminates here is the LOG one: nothing may tell a
    human that a paid shot is being retried, and nothing may enter the code path
    that would.
    """
    plan, sb, render_dir = _scene(tmp_path, monkeypatch,
                                  motion_types=["ai_video"])
    calls = {"paid": 0}

    def dead_paid(ds, synth, sb_, out_dir, log=print):
        calls["paid"] += 1
        raise BrokenPipeError(32, "Broken pipe")

    monkeypatch.setattr(director, "generate_paid_clip", dead_paid)
    monkeypatch.setattr(director, "normalize_clip", lambda *a, **k: None)
    monkeypatch.setattr(director, "fit_clip", lambda *a, **k: None)

    lines, log = _log()
    with pytest.raises(director.PlanError):
        _compile(sb, render_dir, log)

    assert calls["paid"] == 1, (
        f"fal was called {calls['paid']} times for one shot")
    assert not [ln for ln in lines if "retrying" in ln], (
        f"a paid generation was retried: {lines}")


def test_a_failed_still_purchase_is_never_retried(tmp_path, monkeypatch):
    """A `parallax` shot is free to RENDER and not free to draft.

    Every generated shot buys stills through `assets.generate_for_shot` at
    ~$0.15 an image before a single frame is rendered locally, so "free tier"
    describes the motion and not the whole shot. A retry that re-entered the shot
    before those stills were settled would buy them twice, on the tier that is
    supposed to be the cheap one.
    """
    from backend import assets, motion

    plan, sb, render_dir = _scene(tmp_path, monkeypatch,
                                  motion_types=["parallax"],
                                  stills_on_disk=False)
    calls = {"stills": 0, "render": 0}

    def dead_stills(synth, n, **kw):
        calls["stills"] += 1
        raise BrokenPipeError(32, "Broken pipe")

    def fake_render(synth, out_dir, storyboard=None, **kw):
        calls["render"] += 1
        target = Path(out_dir) / f"{synth.scene_id}.mp4"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"FREE-PARALLAX-RENDER")

    monkeypatch.setattr(assets, "generate_for_shot", dead_stills)
    monkeypatch.setattr(motion, "render_shot", fake_render)
    monkeypatch.setattr(director, "normalize_clip", lambda *a, **k: None)
    monkeypatch.setattr(director, "fit_clip", lambda *a, **k: None)

    lines, log = _log()
    with pytest.raises(director.PlanError):
        _compile(sb, render_dir, log)

    assert calls["stills"] == 1, (
        f"the still purchase ran {calls['stills']} times for one shot — a retry "
        f"re-bought the drafts")
    assert not [ln for ln in lines if "retrying" in ln], (
        f"a failed still purchase was announced as retryable: {lines}")


def test_a_retry_reuses_the_stills_that_were_already_bought(tmp_path, monkeypatch):
    """The stills succeed, the local render does not.

    The retry has to come back through the same block that buys them, and it must
    take the reuse arm — which it can only do because `ds.draft_variations` was
    written before the render was attempted.
    """
    from backend import assets, motion

    plan, sb, render_dir = _scene(tmp_path, monkeypatch,
                                  motion_types=["parallax"],
                                  stills_on_disk=False)
    calls = {"stills": 0, "render": 0}

    def fake_stills(synth, n, **kw):
        calls["stills"] += 1
        synth.draft_variations = [f"{synth.scene_id}_v{i}.png" for i in range(n)]
        synth.chosen_variation = 0
        synth.draft_image = synth.draft_variations[0]

    def flaky_render(synth, out_dir, storyboard=None, **kw):
        calls["render"] += 1
        if calls["render"] == 1:
            raise BrokenPipeError(32, "Broken pipe")
        target = Path(out_dir) / f"{synth.scene_id}.mp4"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"FREE-PARALLAX-RENDER")

    monkeypatch.setattr(assets, "generate_for_shot", fake_stills)
    monkeypatch.setattr(motion, "render_shot", flaky_render)
    monkeypatch.setattr(director, "normalize_clip", lambda *a, **k: None)
    monkeypatch.setattr(director, "fit_clip", lambda *a, **k: None)

    lines, log = _log()
    _compile(sb, render_dir, log)

    assert calls["stills"] == 1, (
        f"the drafts were bought {calls['stills']} times because the shot was "
        f"rendered twice")
    assert calls["render"] == 2, "the render was supposed to be retried"


# --- the defect the retry exists for --------------------------------------------

def test_one_transient_ffmpeg_death_no_longer_abandons_the_whole_beat(
        tmp_path, monkeypatch):
    """The reported shape: the penultimate shot of the beat dies, once."""
    from backend import motion

    plan, sb, render_dir = _scene(tmp_path, monkeypatch,
                                  motion_types=["parallax"] * 3)
    rendered: list[str] = []

    def flaky_render(synth, out_dir, storyboard=None, **kw):
        rendered.append(synth.scene_id)
        if synth.scene_id == f"{BEAT}.02" and rendered.count(f"{BEAT}.02") == 1:
            raise BrokenPipeError(32, "Broken pipe")
        target = Path(out_dir) / f"{synth.scene_id}.mp4"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"FREE-PARALLAX-RENDER")

    monkeypatch.setattr(motion, "render_shot", flaky_render)
    monkeypatch.setattr(director, "normalize_clip", lambda *a, **k: None)
    monkeypatch.setattr(director, "fit_clip", lambda *a, **k: None)

    lines, log = _log()
    try:
        _compile(sb, render_dir, log)
    except director.PlanError as exc:      # the defect, stated as it was found
        raise AssertionError(
            f"one transient ffmpeg death abandoned the whole beat: {exc}"
        ) from None

    saved = director.load_plan(BEAT)
    assert saved.status == "compiled", saved.status
    assert [s.error for s in saved.coverage] == ["", "", ""], (
        f"a shot that recovered still carries an error: "
        f"{[(s.id, s.error) for s in saved.coverage]}")
    assert rendered == [f"{BEAT}.01", f"{BEAT}.02", f"{BEAT}.02", f"{BEAT}.03"], (
        f"the retry was not confined to the shot that failed: {rendered}")


def test_a_retry_that_succeeds_is_still_reported(tmp_path, monkeypatch):
    """A silent retry that works would read as a clean first run."""
    from backend import motion

    plan, sb, render_dir = _scene(tmp_path, monkeypatch,
                                  motion_types=["parallax"] * 2)
    calls = {"render": 0}

    def flaky_render(synth, out_dir, storyboard=None, **kw):
        calls["render"] += 1
        if synth.scene_id == f"{BEAT}.01" and calls["render"] == 1:
            raise BrokenPipeError(32, "Broken pipe")
        target = Path(out_dir) / f"{synth.scene_id}.mp4"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"FREE-PARALLAX-RENDER")

    monkeypatch.setattr(motion, "render_shot", flaky_render)
    monkeypatch.setattr(director, "normalize_clip", lambda *a, **k: None)
    monkeypatch.setattr(director, "fit_clip", lambda *a, **k: None)

    lines, log = _log()
    _compile(sb, render_dir, log)

    done = [ln for ln in lines if ln.startswith(f"  {BEAT}: ")]
    assert done and "needed a retry" in done[-1], (
        f"the completion line does not report the retry: {done or lines}")
    assert f"{BEAT}.01" in done[-1], (
        f"the completion line does not name the shot that was retried: {done[-1]}")
    assert [ln for ln in lines if "succeeded on attempt 2" in ln], (
        f"the successful retry was not announced: {lines}")
    assert director.load_plan(BEAT).compiled.get("retried") == [f"{BEAT}.01"]


def test_a_clean_run_does_not_claim_a_retry(tmp_path, monkeypatch):
    """The other direction, so "needed a retry" means something."""
    from backend import motion

    plan, sb, render_dir = _scene(tmp_path, monkeypatch,
                                  motion_types=["parallax"] * 2)

    def fake_render(synth, out_dir, storyboard=None, **kw):
        target = Path(out_dir) / f"{synth.scene_id}.mp4"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"FREE-PARALLAX-RENDER")

    monkeypatch.setattr(motion, "render_shot", fake_render)
    monkeypatch.setattr(director, "normalize_clip", lambda *a, **k: None)
    monkeypatch.setattr(director, "fit_clip", lambda *a, **k: None)

    lines, log = _log()
    _compile(sb, render_dir, log)

    assert not [ln for ln in lines if "retry" in ln or "retrying" in ln], lines
    assert director.load_plan(BEAT).compiled.get("retried") == []


# --- the retry that does not save it --------------------------------------------

def test_a_shot_that_fails_twice_says_so(tmp_path, monkeypatch):
    """A silent retry that FAILS is how somebody debugs a phantom.

    The human has to be able to read, from the log alone, that this shot was
    given two goes and died on both — otherwise the retry is an invisible
    variable in every subsequent diagnosis.
    """
    from backend import motion

    plan, sb, render_dir = _scene(tmp_path, monkeypatch,
                                  motion_types=["parallax"])
    calls = {"render": 0}

    def dead_render(synth, out_dir, storyboard=None, **kw):
        calls["render"] += 1
        raise BrokenPipeError(32, "Broken pipe")

    monkeypatch.setattr(motion, "render_shot", dead_render)
    monkeypatch.setattr(director, "normalize_clip", lambda *a, **k: None)
    monkeypatch.setattr(director, "fit_clip", lambda *a, **k: None)

    lines, log = _log()
    with pytest.raises(director.PlanError):
        _compile(sb, render_dir, log)

    failed = [ln for ln in lines if "FAILED" in ln]
    assert failed and "2 attempts" in failed[-1] and "retried" in failed[-1], (
        f"the final failure does not say the shot was retried: {failed or lines}")
    assert [ln for ln in lines if "attempt 1 failed" in ln], (
        f"the first attempt was never reported: {lines}")
    assert calls["render"] == director.LOCAL_RENDER_ATTEMPTS, (
        f"the shot was attempted {calls['render']} times; the retry is supposed "
        f"to be bounded at {director.LOCAL_RENDER_ATTEMPTS}")
    assert director.load_plan(BEAT).coverage[0].error, (
        "the shot must still carry its error after both attempts")


def test_a_normalize_failure_on_a_free_shot_is_retried(tmp_path, monkeypatch):
    """The retry covers the ffmpeg post-processing too, not only the render.

    Both observed deaths were writes into an ffmpeg child, and `normalize_clip`
    and `fit_clip` are two more of those on the same free-tier shot.
    """
    from backend import motion

    plan, sb, render_dir = _scene(tmp_path, monkeypatch,
                                  motion_types=["parallax"])
    calls = {"normalize": 0}

    def fake_render(synth, out_dir, storyboard=None, **kw):
        target = Path(out_dir) / f"{synth.scene_id}.mp4"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"FREE-PARALLAX-RENDER")

    def flaky_normalize(path, log=print):
        calls["normalize"] += 1
        if calls["normalize"] == 1:
            raise BrokenPipeError(32, "Broken pipe")

    monkeypatch.setattr(motion, "render_shot", fake_render)
    monkeypatch.setattr(director, "normalize_clip", flaky_normalize)
    monkeypatch.setattr(director, "fit_clip", lambda *a, **k: None)

    lines, log = _log()
    try:
        _compile(sb, render_dir, log)
    except director.PlanError as exc:
        raise AssertionError(
            f"a transient normalize failure abandoned the beat: {exc}") from None

    assert calls["normalize"] == 2
    assert director.load_plan(BEAT).status == "compiled"


def test_a_library_shot_is_not_re_recorded_by_a_retry(tmp_path, monkeypatch):
    """A library copy is free, but its ledger row is written once.

    The retry boundary for that branch therefore sits AFTER
    `ledger.record_generation`, so a second attempt covers the ffmpeg calls that
    actually break and not the provenance record, which would otherwise gain a
    duplicate row for one reused asset.
    """
    plan, sb, render_dir = _scene(tmp_path, monkeypatch,
                                  motion_types=["parallax"])
    saved = director.load_plan(BEAT)
    src = tmp_path / "library_asset.mp4"
    src.write_bytes(b"LIBRARY-BYTES")
    saved.coverage[0].source = "library"
    saved.coverage[0].source_ref = str(src)
    director.save_plan(saved)

    calls = {"ledger": 0, "normalize": 0}
    monkeypatch.setattr(director.ledger, "record_generation",
                        lambda **kw: calls.__setitem__("ledger",
                                                       calls["ledger"] + 1))

    def flaky_normalize(path, log=print):
        calls["normalize"] += 1
        if calls["normalize"] == 1:
            raise BrokenPipeError(32, "Broken pipe")

    monkeypatch.setattr(director, "normalize_clip", flaky_normalize)
    monkeypatch.setattr(director, "fit_clip", lambda *a, **k: None)

    lines, log = _log()
    _compile(sb, render_dir, log)

    assert calls["ledger"] == 1, (
        f"one reused asset produced {calls['ledger']} provenance rows")
    assert calls["normalize"] == 2, "the ffmpeg step was supposed to be retried"
