"""POST /api/director/shot/{id} — the sanctioned shot-edit path.

This endpoint had NO tests, which is why a regression that 400'd the Motion
Technique buttons and take selection shipped with a green suite. The tier control
is the Gate-1 budget allocator, so breaking it broke the one edit that decides
paid spend.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
for m in ("anthropic", "fal_client", "elevenlabs"):
    sys.modules.setdefault(m, types.ModuleType(m))

pytest.importorskip("fastapi.testclient")

from backend import director  # noqa: E402


@pytest.fixture
def plan(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from backend import main as M
    from backend.manifest import Camera, Shot, Storyboard

    monkeypatch.setattr(director.config, "MANIFEST_PATH",
                        tmp_path / "storyboard_manifest.json")
    p = director.CoveragePlan(beat_id="s011", beat_duration=12.0, status="draft")
    p.coverage = [
        director.DirectorShot(id="s011.01", beat_id="s011", motion_type="parallax",
                              camera=Camera(move="static", duration=6.0)),
        director.DirectorShot(id="s011.02", beat_id="s011", motion_type="parallax",
                              camera=Camera(move="static", duration=6.0)),
    ]
    p.coverage[0].draft_variations = ["a.png", "b.png", "c.png"]
    director.save_plan(p)

    sb = Storyboard(title="T", storyboard_approved=True,
                    shots=[Shot(scene_id="s011", narration="n", prompt="p",
                                camera=Camera(move="static", duration=12.0))])
    monkeypatch.setattr(M, "get_current_project", lambda: sb)
    return TestClient(M.app)


def test_motion_tier_change_is_accepted_with_the_cost_the_client_echoes(plan):
    """The Motion Technique buttons send estimated_cost beside motion_type. That
    is a field the SERVER owns and recomputes -- dropping it is right, 400ing on
    it killed the Tier-C budget control entirely."""
    r = plan.post("/api/director/shot/s011.01",
                  json={"motion_type": "ai_video", "estimated_cost": 1.25})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["shot"]["motion_type"] == "ai_video"
    # The server's own number, not the client's 1.25 literal. `!= 1.25 or > 0`
    # was tautological -- it could not fail for any value.
    cost = body["shot"]["estimated_cost"]
    assert cost > 0, "a paid shot must carry a cost"
    assert cost != 1.25, "the client's literal must not survive as the stored cost"


def test_take_selection_persists(plan):
    r = plan.post("/api/director/shot/s011.01", json={"chosen_variation": 2})
    assert r.status_code == 200, r.text
    assert r.json()["shot"]["chosen_variation"] == 2
    assert director.load_plan("s011").coverage[0].chosen_variation == 2


def test_a_take_that_does_not_exist_is_refused(plan):
    r = plan.post("/api/director/shot/s011.01", json={"chosen_variation": 9})
    assert r.status_code == 400
    assert "does not exist" in r.json()["detail"]


def test_the_nested_camera_shape_the_drawer_sends_is_accepted(plan):
    """The drawer posted {"camera":{"duration":..}} while the endpoint read a flat
    key, so the slider round-tripped a 200 and changed nothing."""
    r = plan.post("/api/director/shot/s011.01", json={"camera": {"duration": 4.0}})
    assert r.status_code == 200, r.text
    assert director.load_plan("s011").coverage[0].camera.duration == 4.0


def test_a_genuinely_unknown_field_is_still_refused(plan):
    """The strictness is worth keeping -- it just must not fire on server-owned
    fields. Silently ignoring unknown keys is what hid two client bugs."""
    r = plan.post("/api/director/shot/s011.01", json={"colour_grade": "warm"})
    assert r.status_code == 400
    assert "colour_grade" in r.json()["detail"]


def test_lengthening_a_shot_takes_the_time_from_its_siblings(plan):
    """Coverage must still sum to the beat or the plan cannot compile."""
    r = plan.post("/api/director/shot/s011.01", json={"duration": 8.0})
    assert r.status_code == 200, r.text
    fresh = director.load_plan("s011")
    assert abs(sum(s.duration for s in fresh.coverage) - 12.0) < 0.1


def test_a_sibling_at_the_floor_is_never_lengthened_to_pay_for_another(plan):
    """room = o.duration - MIN_SHOT_SECONDS went negative for a sibling already at
    the floor, so `take` was negative and the sibling GREW -- widening the very
    shortfall the loop was closing."""
    # 1.0, deliberately BELOW MIN_SHOT_SECONDS (1.2). At exactly 1.2 the old
    # expression (o.duration - 1.2 = 0.0) and the new one (max(0.0, 0.0)) agree,
    # so this test passed against the unfixed code -- it certified a fix it did
    # not exercise. Only a sibling below the floor makes `room` negative.
    p = director.load_plan("s011")
    p.coverage[1].camera.duration = 1.0
    p.coverage[0].camera.duration = 11.0
    director.save_plan(p)

    plan.post("/api/director/shot/s011.01", json={"duration": 11.5})
    fresh = director.load_plan("s011")
    assert fresh.coverage[1].duration <= 1.0 + 0.01, (
        f"a sibling below the floor must never be LENGTHENED to pay for another "
        f"(got {fresh.coverage[1].duration})")


def test_take_indices_are_zero_based_first_and_last(plan):
    """chosen_variation indexes draft_variations directly in director.py:811, and
    that still is what gets uploaded to the paid image-to-video model. The Take
    Selector stored idx + 1, so picking TAKE 1 animated take 2 and TAKE 4 of 4 was
    rejected as out of range."""
    p = director.load_plan("s011")
    p.coverage[0].draft_variations = ["a.png", "b.png", "c.png", "d.png"]
    director.save_plan(p)

    # First tile.
    assert plan.post("/api/director/shot/s011.01",
                     json={"chosen_variation": 0}).status_code == 200
    assert director.load_plan("s011").coverage[0].chosen_variation == 0

    # Last tile of four — this is the one the 1-based modal could never send.
    assert plan.post("/api/director/shot/s011.01",
                     json={"chosen_variation": 3}).status_code == 200
    assert director.load_plan("s011").coverage[0].chosen_variation == 3

    # One past the end is still refused.
    assert plan.post("/api/director/shot/s011.01",
                     json={"chosen_variation": 4}).status_code == 400


def test_a_shot_with_no_drafts_has_no_take_zero(plan):
    """max(1, len(...)) let take 0 through on a shot with nothing to index."""
    p = director.load_plan("s011")
    p.coverage[0].draft_variations = []
    director.save_plan(p)
    r = plan.post("/api/director/shot/s011.01", json={"chosen_variation": 0})
    assert r.status_code == 400


def test_a_duration_change_re_prices_and_re_routes_a_paid_shot(plan):
    """Price and model both depend on length, so a duration edit invalidates them
    exactly as a tier edit does. Only the tier branch re-ran the router, so
    dragging 4s -> 10s left the shot priced and routed for 4s -- under-reporting
    the cost on the very screen where the Gate-1 budget is allocated.

    10.8s rather than the 10.0s this first used, and the reason is worth keeping.
    At fal's published rates kling_2_1_standard is the cheapest model at BOTH 6s
    and 10s -- it only offers 5s and 10s, so a 6s shot buys 10s of kling at
    $0.056/s and trims, which still undercuts wan_2_7 at $0.10/s. So 6 -> 10
    changes neither the model nor the price, correctly, and this test's assertions
    could not fail for any implementation. 10.8s passes kling's ceiling entirely,
    which is where a stale route and a stale price are both observable.
    """
    plan.post("/api/director/shot/s011.01", json={"motion_type": "ai_video"})
    at_6s = director.load_plan("s011").coverage[0]

    r = plan.post("/api/director/shot/s011.01", json={"duration": 10.8})
    assert r.status_code == 200, r.text
    after = director.load_plan("s011").coverage[0]
    assert after.camera.duration == 10.8

    assert after.backend != at_6s.backend, (
        f"the shot stayed on {at_6s.backend} past that model's 10s ceiling")
    assert after.estimated_cost > at_6s.estimated_cost, (
        f"a 6s->10.8s change left the cost at ${at_6s.estimated_cost} "
        f"(backend {at_6s.backend} -> {after.backend})")


def test_a_free_shot_is_not_charged_a_video_rate_after_a_resize(plan):
    plan.post("/api/director/shot/s011.01", json={"motion_type": "parallax"})
    plan.post("/api/director/shot/s011.01", json={"duration": 9.0})
    after = director.load_plan("s011").coverage[0]
    assert after.backend == ""
    assert after.estimated_cost == pytest.approx(0.15, abs=0.001)


def test_a_non_routing_edit_is_not_refused_for_a_stale_duration(plan):
    """Strictness keyed on identity, not on what the body asked for. Once a
    rebalance had left this shot at an unroutable length, selecting a take or
    editing a prompt 400'd citing a duration the user never typed -- killing the
    Take Selector by a different route."""
    p = director.load_plan("s011")
    p.coverage[0].motion_type = "ai_video"
    # 1.5s gestural is genuinely unroutable: wan_2_7 has the lowest floor at 2.0s
    # and gestural forbids rounding up. 2.0 -- which this test first used -- IS
    # wan's floor and routes fine, so the test passed against the unfixed code.
    p.coverage[0].camera.duration = 1.5
    p.coverage[0].gestural = True
    p.coverage[0].draft_variations = ["a.png", "b.png", "c.png"]
    director.save_plan(p)

    for body in ({"chosen_variation": 1}, {"prompt": "a different subject"},
                 {"identity_critical": True}, {"shot_size": "cu"}):
        r = plan.post("/api/director/shot/s011.01", json=body)
        assert r.status_code == 200, f"{body} -> {r.status_code} {r.text[:140]}"


def test_a_routing_edit_on_an_unproducible_length_is_still_refused(plan):
    """The strictness is worth keeping where it belongs."""
    plan.post("/api/director/shot/s011.01", json={"motion_type": "ai_video"})
    p = director.load_plan("s011")
    p.coverage[0].gestural = True
    director.save_plan(p)
    r = plan.post("/api/director/shot/s011.01", json={"duration": 1.4})
    assert r.status_code == 400
    assert "no model" in (r.json().get("error") or "")


def test_an_unroutable_sibling_does_not_keep_a_price_it_cannot_be_made_at(plan):
    """planner.scene_summary sums estimated_cost into the Gate-1 total, and
    director.validate does not check routability -- so a stale price on an
    ungeneratable shot was approved as budget and only failed at compile."""
    p = director.load_plan("s011")
    for c in p.coverage:
        c.motion_type = "ai_video"
    p.coverage[1].gestural = True
    p.coverage[1].backend = "wan_2_7"
    p.coverage[1].estimated_cost = 0.51
    director.save_plan(p)

    # Grow shot 1 so the rebalance drives the sibling under wan_2_7's 2.0s floor,
    # the lowest of any configured model. Asserted, not `if`-guarded: a setup that
    # quietly fails to reach the state under test is how a vacuous test happens.
    plan.post("/api/director/shot/s011.01", json={"duration": 10.8})
    sib = director.load_plan("s011").coverage[1]
    assert sib.duration < 2.0, (
        f"setup did not reach the unroutable state (sibling at {sib.duration}s)")
    assert sib.backend == "", "a shot no model can make must not name one"
    assert sib.estimated_cost <= 0.16, (
        f"it must not still be priced at ${sib.estimated_cost} for a length it "
        f"cannot be generated at — that figure is summed into the Gate-1 total")
    assert "no_legal_backend" in sib.constrained_by


def test_the_patch_response_carries_the_resolved_thumbnail(plan):
    """The list endpoint resolves thumbnail_url; the patch response did not, so
    setSelectedShot(res.shot) blanked the image after every edit."""
    p = director.load_plan("s011")
    p.coverage[0].draft_variations = ["a.png", "b.png"]
    p.coverage[0].chosen_variation = 1
    director.save_plan(p)
    r = plan.post("/api/director/shot/s011.01", json={"shot_size": "cu"})
    assert r.status_code == 200
    assert r.json()["shot"]["thumbnail_url"] == "b.png"
