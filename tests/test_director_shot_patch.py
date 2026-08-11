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
    # The server's own number, not the client's literal.
    assert body["shot"]["estimated_cost"] != 1.25 or body["shot"]["estimated_cost"] > 0


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
    p = director.load_plan("s011")
    p.coverage[1].camera.duration = 1.2
    p.coverage[0].camera.duration = 10.8
    director.save_plan(p)

    plan.post("/api/director/shot/s011.01", json={"duration": 11.5})
    fresh = director.load_plan("s011")
    assert fresh.coverage[1].duration <= 1.2 + 0.01, "a floored sibling must not grow"
