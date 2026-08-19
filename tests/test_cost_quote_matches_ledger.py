"""The number on the consent button is the number that gets recorded.

Gate 1 exists so a human allocates the render budget knowing what it costs. The
first real end-to-end compile this pipeline ever ran put "COMPILE & SPEND $1.99"
on that button, quoting $0.40 and $0.39 for its two paid shots, and the
generation ledger then recorded $0.60 for each of them.

Two independent prices was the whole defect. The quote came from
``capabilities.cost_per_second * seconds``; the charge came from a flat
``PAID_CLIP_COST = 0.60`` in director.py. Nothing compared them, because nothing
could -- they lived on opposite sides of the consent boundary.

An overstated quote costs a human a moment's hesitation. An understated one takes
consent for one amount and spends another.

EVERY assertion here compares the QUOTE to the LEDGER. Comparing a quote to
another estimate is what let this survive: two numbers computed the same way
agree with each other and say nothing about the money. The quote is always read
back from the surfaces the studio reads -- ``POST /api/director/shot`` prices it,
``GET /api/director/scene`` reports it -- and the spend always from
``generation.spend``, which is what the recovery routes and the studio's own
spend panel report.
"""

from __future__ import annotations

import json
import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
for m in ("anthropic", "fal_client", "elevenlabs"):
    sys.modules.setdefault(m, types.ModuleType(m))

pytest.importorskip("fastapi.testclient")

from backend import capabilities, director, generation  # noqa: E402

BEAT = "s011"
SHOT = "s011.01"


def _scene(tmp_path, monkeypatch, *, seconds: float, backend: str = "",
           identity_critical: bool = False, stills_on_disk: bool = False):
    """One beat, one shot, a faked provider, and a real ledger on disk.

    Nothing here reaches fal. What is exercised is the accounting either side of
    the call: what the studio quotes before it, and what the ledger records
    after.
    """
    from fastapi.testclient import TestClient

    from backend import assets, main as M, motion
    from backend.manifest import Camera, Shot, Storyboard

    monkeypatch.setattr(director.config, "MANIFEST_PATH",
                        tmp_path / "storyboard_manifest.json")
    render_dir = tmp_path / "render"
    render_dir.mkdir(parents=True, exist_ok=True)

    plan = director.CoveragePlan(beat_id=BEAT, beat_duration=seconds, status="draft")
    ds = director.DirectorShot(
        id=SHOT, beat_id=BEAT, motion_type="parallax", backend=backend,
        camera=Camera(move="static", duration=seconds),
        identity_critical=identity_critical,
        prompt="a hand on drill steel", subject="single jack",
        shot_size="m", purpose="master")
    if stills_on_disk:
        ds.draft_variations = ["a.png"]
        ds.chosen_variation = 0
    plan.coverage = [ds]
    director.save_plan(plan)

    sb = Storyboard(title="T", storyboard_approved=True,
                    shots=[Shot(scene_id=BEAT, narration="n", prompt="p",
                                camera=Camera(move="static", duration=seconds))])
    monkeypatch.setattr(M, "get_current_project", lambda: sb)

    calls = {"paid": 0, "stills": 0}

    def fake_paid(ds_, synth, sb_, out_dir, log=print, on_billed=None):
        calls["paid"] += 1
        target = Path(out_dir) / f"{ds_.id}.mp4"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"PAID-BYTES")
        return target

    def fake_stills(synth, n, **kw):
        calls["stills"] += n
        synth.draft_variations = [f"{synth.scene_id}_v{i}.png" for i in range(n)]
        synth.chosen_variation = 0
        synth.draft_image = synth.draft_variations[0]

    def fake_render(synth, out_dir, storyboard=None, **kw):
        target = Path(out_dir) / f"{synth.scene_id}.mp4"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"FREE-PARALLAX-RENDER")

    monkeypatch.setattr(director, "generate_paid_clip", fake_paid)
    monkeypatch.setattr(assets, "generate_for_shot", fake_stills)
    monkeypatch.setattr(motion, "render_shot", fake_render)
    monkeypatch.setattr(director, "normalize_clip", lambda *a, **k: None)
    monkeypatch.setattr(director, "fit_clip", lambda *a, **k: None)
    monkeypatch.setattr(director, "concat", lambda *a, **k: Path("beat.mp4"))

    return TestClient(M.app), sb, render_dir, calls


def _quote(client) -> float:
    """The figure the studio puts on the button, read from the studio's endpoint."""
    r = client.get(f"/api/director/scene?beats={BEAT}")
    assert r.status_code == 200, r.text
    return float(r.json()["summary"]["estimated_cost"])


def _price_as_paid_video(client) -> None:
    """Set the tier through the Gate-1 budget control, so the SERVER prices it."""
    r = client.post(f"/api/director/shot/{SHOT}", json={"motion_type": "ai_video"})
    assert r.status_code == 200, r.text


def _compile(sb, render_dir):
    plan = director.load_plan(BEAT)
    director.approve(plan)
    plan.status = "locked"
    director.save_plan(plan)
    director.compile_coverage(director.load_plan(BEAT), sb, render_dir,
                              log=lambda m: None)


# --- the defect, in the units it was found in -----------------------------------
#
# 5.0s routes to kling_2_1_standard and 2.0s to wan_2_7 -- the two backends the
# deployed run actually billed. Both are parametrised rather than asserted once,
# because a per-model price table drifts per model.
#
# 2.0s, not the 3.0s this first used: at fal's published rates kling costs
# $0.056/s against wan's $0.10/s, so kling wins any length it can serve. wan is
# only the cheapest below kling's 5s floor (2s of wan is $0.20 against kling's
# $0.28 minimum) or above its 10s ceiling. The router's assertion at the end of
# the test is what catches this case silently ceasing to cover wan at all.

@pytest.mark.parametrize("seconds,expect_backend", [
    (5.0, "kling_2_1_standard"),
    (2.0, "wan_2_7"),
])
def test_a_paid_shot_is_never_quoted_below_what_the_ledger_records(
        tmp_path, monkeypatch, seconds, expect_backend):
    """THE defect. Quoted $0.40, recorded $0.60, and nothing noticed."""
    client, sb, render_dir, calls = _scene(tmp_path, monkeypatch, seconds=seconds)
    _price_as_paid_video(client)
    quoted = _quote(client)

    _compile(sb, render_dir)
    spend = generation.spend(BEAT)

    # First, because it is the one that fails on the unfixed code and every
    # other assertion in this test is context for it.
    assert quoted >= spend["spent"], (
        f"the human consented to ${quoted:.2f} and the ledger recorded "
        f"${spend['spent']:.2f} — consent was taken for one amount and another "
        f"was spent")
    assert spend["spent"] > 0, "the compile recorded no spend at all; nothing was compared"
    assert spend["spend_is_certain"], spend["summary"]
    assert calls["paid"] == 1
    assert director.load_plan(BEAT).coverage[0].backend == expect_backend, (
        "this case no longer exercises the model it was written for")


def test_every_configured_video_model_is_quoted_at_or_above_what_it_records(
        tmp_path, monkeypatch):
    """The same question asked of the whole registry, not just the two observed.

    A price table drifts one row at a time, so a model nobody has run yet is
    exactly where the next understatement lives. Each model is compiled for real
    against its own default duration and the ledger is read back.
    """
    understated = []
    for key, caps in capabilities.VIDEO_CAPS.items():
        seconds = float(caps["min_seconds"])
        client, sb, render_dir, _ = _scene(
            tmp_path / key, monkeypatch, seconds=seconds, backend=key)
        _price_as_paid_video(client)
        # _reprice routes to the cheapest legal model; pin it back to the one
        # under test, then re-read the quote for that choice.
        plan = director.load_plan(BEAT)
        plan.coverage[0].backend = key
        plan.coverage[0].estimated_cost = director.quote_shot(plan.coverage[0])
        director.save_plan(plan)
        quoted = _quote(client)

        _compile(sb, render_dir)
        recorded = generation.spend(BEAT)["spent"]
        if quoted < recorded:
            understated.append(f"{key}: quoted ${quoted:.2f}, recorded ${recorded:.2f}")

    assert not understated, (
        "these models quote a human less than the ledger then records for "
        f"them: {understated}")


def test_the_stills_a_shot_buys_are_quoted_and_recorded_as_the_same_number(
        tmp_path, monkeypatch):
    """identity_critical buys FOUR stills and was quoted for one.

    The same defect as the video tier at 4x, on the shots where likeness matters
    most, and it survived because the quote hardcoded a single image while the
    compile read ``4 if ds.identity_critical``.
    """
    client, sb, render_dir, calls = _scene(tmp_path, monkeypatch, seconds=5.0,
                                           identity_critical=True)
    quoted = _quote(client)          # still parallax: stills only, no clip

    _compile(sb, render_dir)
    spend = generation.spend(BEAT)

    assert quoted >= spend["spent"], (
        f"an identity-critical shot was quoted ${quoted:.2f} and bought "
        f"${spend['spent']:.2f} of stills")
    assert calls["stills"] == 4, "this test no longer exercises the four-take path"
    assert spend["spent"] == pytest.approx(4 * capabilities.COST_PER_IMAGE, abs=0.001)


def test_stills_bought_by_a_compile_reach_the_ledger_at_all(tmp_path, monkeypatch):
    """A spend the ledger cannot see is a spend nobody can reconcile (§6.1).

    compile_coverage called assets.generate_for_shot directly and opened no
    attempt, so a run that bought ten stills reported only its video tier.
    """
    client, sb, render_dir, calls = _scene(tmp_path, monkeypatch, seconds=5.0)
    _compile(sb, render_dir)

    spend = generation.spend(BEAT)
    assert spend["spent"] == pytest.approx(capabilities.COST_PER_IMAGE, abs=0.001), (
        f"the compile bought {calls['stills']} still(s) and the ledger reports "
        f"${spend['spent']:.2f}")
    assert spend["paid_attempts"] == 1


# --- the second defect: the estimate moves after the money is spent --------------

def test_compiling_a_beat_does_not_change_what_it_was_quoted(tmp_path, monkeypatch):
    """A forward estimate and a record of spend must not share a field.

    The compile ADDED its spend into estimated_cost, so the plan a human approved
    at $1.99 reported $4.69 the moment it finished — 2.4x the agreed figure, on
    ten shots that had not changed, and the number a re-compile would quote next.

    Asserted against the plan FILE, not through load_plan. load_plan re-derives
    estimated_cost on read, which is what repairs the plans already on disk — and
    it would also quietly repair a re-introduced ``+=`` before any test could see
    it.
    """
    client, sb, render_dir, _ = _scene(tmp_path, monkeypatch, seconds=5.0)
    _price_as_paid_video(client)
    quoted_before = _quote(client)

    _compile(sb, render_dir)
    on_disk = json.loads(director.plan_path(BEAT).read_text(encoding="utf-8"))
    written = sum(float(s["estimated_cost"]) for s in on_disk["coverage"])

    assert written == pytest.approx(quoted_before, abs=0.001), (
        f"the compile rewrote the quote from ${quoted_before:.2f} to "
        f"${written:.2f} without a single shot changing")
    assert _quote(client) == pytest.approx(quoted_before, abs=0.001)


def test_a_compiled_beat_reports_spend_and_estimate_as_separate_facts(
        tmp_path, monkeypatch):
    """§6.1 asks for "spent so far" and "estimated remaining" as different rows."""
    client, sb, render_dir, _ = _scene(tmp_path, monkeypatch, seconds=5.0)
    _price_as_paid_video(client)
    _compile(sb, render_dir)

    summary = client.get(f"/api/director/scene?beats={BEAT}").json()["summary"]
    assert summary["spend"]["spent"] > 0, "a compiled beat reports no spend"
    assert summary["estimated_cost"] >= summary["spend"]["spent"]
    # The at-risk figure travels with the total, always. A caller rendering
    # `spent` alone is how a charge reports $0.00 again.
    assert "at_risk" in summary["spend"]
    assert summary["spend"]["spend_is_certain"] is True


def test_an_estimate_polluted_by_an_older_compile_is_repaired_on_read(
        tmp_path, monkeypatch):
    """The plans already on disk carry the doubled figure. $4.69 is sitting in a
    real project right now; deriving on read is what un-sticks it, and estimated
    cost is derived from fields the file already holds, so nothing is invented."""
    client, sb, render_dir, _ = _scene(tmp_path, monkeypatch, seconds=5.0)
    _price_as_paid_video(client)
    honest = _quote(client)

    plan = director.load_plan(BEAT)
    plan.coverage[0].estimated_cost = honest + 2.70    # as a pre-fix compile left it
    director.save_plan(plan)

    assert _quote(client) == pytest.approx(honest, abs=0.001), (
        "a plan compiled before the fix keeps quoting the doubled figure")


# --- the price itself has one source ---------------------------------------------

def test_the_quote_and_the_charge_come_from_one_function(tmp_path, monkeypatch):
    """Not a style point. Two hardcoded prices is the defect, and a test that
    only checks today's numbers would pass again the moment a third appears.

    Moving the ONE price source — fal's published rate for the chosen model —
    must move both sides together. If either still assembled its own figure,
    only one of them would follow. The rate is driven to $2.50/s, far outside
    anything in the table, so a stale constant anywhere cannot coincidentally
    agree with it.
    """
    client, sb, render_dir, _ = _scene(tmp_path, monkeypatch, seconds=5.0)
    _price_as_paid_video(client)

    routed = director.load_plan(BEAT).coverage[0].backend
    monkeypatch.setitem(capabilities.VIDEO_CAPS[routed], "cost_per_second", 2.50)
    plan = director.load_plan(BEAT)
    plan.coverage[0].estimated_cost = director.quote_shot(plan.coverage[0])
    director.save_plan(plan)
    quoted = _quote(client)

    _compile(sb, render_dir)
    spend = generation.spend(BEAT)

    assert quoted >= spend["spent"], (
        f"the price source moved to $2.50/s; the quote went to ${quoted:.2f} "
        f"and the ledger to ${spend['spent']:.2f} — they are not the same source")
    assert spend["spent"] >= 2.50, (
        "the charge ignored the price source entirely, so the agreement above "
        "proves nothing")


def test_no_attempt_books_our_own_estimate_as_the_provider_s_bill(
        tmp_path, monkeypatch):
    """`cost` means "the provider told us". Nothing here is ever told.

    GenerationAttempt separates `cost` ("the real one when the provider reported
    it") from `estimated_cost` ("the price the attempt was opened for"), and
    `_amount()` prefers the first. The compile wrote its own figure into `cost`,
    so every settled attempt read as a confirmed invoice and the distinction
    between the two fields meant nothing — the ledger was storing a guess and
    presenting it as a bill.

    fal reports no billed amount on this path: ``fal_client.subscribe`` returns
    the model's output payload and exposes neither a cost field nor the response
    headers. So the honest record is `cost = 0` with the figure left in
    `estimated_cost`, where it is labelled as ours. The TOTAL is unchanged —
    `_amount()` falls back — which is the point: this costs nothing to be honest
    about.
    """
    client, sb, render_dir, _ = _scene(tmp_path, monkeypatch, seconds=5.0)
    _price_as_paid_video(client)
    _compile(sb, render_dir)

    paid = [a for a in generation.load_attempts(BEAT) if a.paid]
    assert paid, "nothing was recorded; there is no invariant to check"

    invented = [f"{a.id} ({a.kind}) books cost=${a.cost:.2f}" for a in paid if a.cost]
    assert not invented, (
        "these attempts present an estimate of ours as a figure the provider "
        f"reported: {invented}")
    assert all(a.estimated_cost > 0 for a in paid), (
        "moving the figure out of `cost` dropped it entirely — that is a "
        "confident $0.00, which is worse than the mislabelling it replaced")
    assert generation.spend(BEAT)["spent"] > 0, (
        "the total must be unaffected; only the label changes")


def test_money_at_risk_is_reported_at_the_price_it_was_quoted_at(
        tmp_path, monkeypatch):
    """The crash path, where the pre-dispatch estimate is the ONLY figure.

    Once a generation settles, ``cost`` overwrites ``estimated_cost`` in every
    total -- so a wrong pre-dispatch price is invisible on the happy path and
    every other test here misses it. It is visible exactly where it matters: the
    provider was called, nobody recorded what it did, and the number the studio
    shows as exposure is whatever was written before dispatch.

    15s is deliberate: wan_2_7 at 15s costs $0.90, comfortably ABOVE the minimum
    per-clip charge, so a hardcoded $0.60 here is distinguishable from a derived
    price. At 5s both are $0.60 and this test could not fail.
    """
    client, sb, render_dir, _ = _scene(tmp_path, monkeypatch, seconds=15.0)
    _price_as_paid_video(client)
    quoted = _quote(client)

    def die(*a, **k):
        raise RuntimeError("connection reset while waiting on the provider")

    monkeypatch.setattr(director, "generate_paid_clip", die)
    with pytest.raises(director.PlanError):
        _compile(sb, render_dir)

    spend = generation.spend(BEAT)
    # The clip's share of what the human agreed to, taken from the quote rather
    # than from a constant of this test's own.
    quoted_clip = quoted - capabilities.COST_PER_IMAGE

    assert spend["at_risk"] == pytest.approx(quoted_clip, abs=0.001), (
        f"the human was quoted ${quoted_clip:.2f} for this clip and the ledger "
        f"reports ${spend['at_risk']:.2f} may have gone")
    assert spend["spend_is_certain"] is False
    assert spend["at_risk_attempts"] == 1


def test_the_planner_prices_a_fresh_plan_the_way_the_compile_pays_for_it(
        tmp_path, monkeypatch):
    """Where the quote is BORN, with the model stubbed out.

    The planner assembled the figure inline -- one image plus the router's number
    -- which is the third hand-rolled copy of a price in this codebase and the
    reason a fourth would go unnoticed. load_plan re-derives on read, so a wrong
    number here is invisible through every read path; what it still reaches is
    ``ledger.record_plan``, the provenance the taste data is built on, and the
    line the operator sees in the planning log.
    """
    from backend import planner

    monkeypatch.setattr(director.config, "MANIFEST_PATH",
                        tmp_path / "storyboard_manifest.json")
    from backend.manifest import Camera, Shot, Storyboard
    sb = Storyboard(title="T", shots=[
        Shot(scene_id=BEAT, narration="n", prompt="p",
             camera=Camera(move="static", duration=10.0))])

    raw = {
        "visual_strategy": "s", "blocking": {},
        "beats": [{"beat_id": BEAT, "shots": [
            {"purpose": "master", "subject": "a face", "shot_size": "cu",
             "angle": "front", "composition": "c", "weight": 1,
             "camera_move": "static", "motion_type": "ai_video",
             "character_motion": True, "face_visibility": "high",
             "motion_complexity": "low", "gestural": False,
             "identity_critical": True, "prompt": "p", "motion_prompt": "m",
             "reason": "r"},
        ]}],
    }
    monkeypatch.setattr(planner, "_client", _fake_anthropic(raw))

    planner.plan_scene(sb, [BEAT], log=lambda m: None)

    # Read from the FILE, before load_plan can re-derive it: the point is what
    # the planner itself wrote.
    on_disk = json.loads(director.plan_path(BEAT).read_text(encoding="utf-8"))
    written = float(on_disk["coverage"][0]["estimated_cost"])
    shot = director.load_plan(BEAT).coverage[0]

    assert written == pytest.approx(director.quote_shot(shot), abs=0.001), (
        f"the planner wrote ${written:.2f} where the compile will pay "
        f"${director.quote_shot(shot):.2f}")
    # Non-vacuous: an identity-critical paid shot costs strictly more than the
    # single image the inline sum used to assume.
    assert written > capabilities.COST_PER_IMAGE


def _fake_anthropic(payload: dict):
    """A stand-in for the Anthropic client that returns one canned JSON block."""
    class _Block:
        type = "text"
        text = json.dumps(payload)

    class _Msg:
        content = [_Block()]
        stop_reason = "end_turn"

    class _Stream:
        text_stream = iter(())

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get_final_message(self):
            return _Msg()

    class _Messages:
        def stream(self, **kw):
            return _Stream()

    class _Client:
        messages = _Messages()

    return lambda: _Client()


def test_an_unproducible_paid_shot_is_never_recorded_at_a_confident_zero(
        tmp_path, monkeypatch):
    """No model can serve it, so nothing may be bought for it — and nothing may
    open a paid attempt carrying $0.00 either. A confident zero on money is the
    answer this codebase refuses everywhere else (see generation.unknown_spend).
    """
    client, sb, render_dir, calls = _scene(tmp_path, monkeypatch, seconds=45.0,
                                           stills_on_disk=True)
    plan = director.load_plan(BEAT)
    plan.coverage[0].motion_type = "ai_video"
    director.save_plan(plan)

    with pytest.raises(director.PlanError):
        _compile(sb, render_dir)

    assert calls["paid"] == 0, "fal was called for a shot no model can produce"
    assert generation.spend(BEAT)["spent"] == 0.0
    assert not [a for a in generation.load_attempts(BEAT) if a.paid], (
        "a paid attempt was opened for a generation that could never be made")
