"""Approval bound to an exact plan version (contract §5.4, §11.5).

The failure this prevents is approval drift: `status == "locked"` records that a
human acted, but on its own it cannot say *what* they acted on, so any later
edit inherits the approval silently. Nothing raises, and the plan that renders
is not the plan anyone approved.

These tests are about the boundary between changes that do and do not
invalidate an approval, because getting either side wrong is its own failure —
too strict and every take selection sends the user back to Direct, too loose and
approval means nothing.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
for _m in ("anthropic", "fal_client", "elevenlabs"):
    sys.modules.setdefault(_m, types.ModuleType(_m))

pytest.importorskip("fastapi.testclient")

from backend import director  # noqa: E402
from backend.manifest import Camera, Shot, Storyboard  # noqa: E402


@pytest.fixture
def studio(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from backend import main as M

    monkeypatch.setattr(director.config, "MANIFEST_PATH",
                        tmp_path / "storyboard_manifest.json")
    dispatched: list[str] = []
    monkeypatch.setattr(M, "start_job",
                        lambda name, fn, *a, **k: (dispatched.append(name), True)[1])
    sb = Storyboard(title="T", script_locked=True, storyboard_approved=True,
                    shots=[Shot(scene_id="s001", narration="n", prompt="p",
                                camera=Camera(move="static", duration=12.0))])
    monkeypatch.setattr(M, "get_current_project", lambda: sb)
    return TestClient(M.app, raise_server_exceptions=False), dispatched


def _plan(status: str = "draft") -> director.CoveragePlan:
    p = director.CoveragePlan(beat_id="s001", beat_duration=12.0, status=status)
    p.coverage = [
        director.DirectorShot(id="s001.01", beat_id="s001", motion_type="parallax",
                              prompt="a", shot_size="ws",
                              camera=Camera(move="static", duration=6.0)),
        director.DirectorShot(id="s001.02", beat_id="s001", motion_type="parallax",
                              prompt="b", shot_size="cu",
                              camera=Camera(move="static", duration=6.0)),
    ]
    director.save_plan(p)
    return p


# --- what the signature covers ---------------------------------------------------

def test_the_same_plan_signs_the_same_way():
    assert director.plan_signature(_plan_unsaved()) == director.plan_signature(_plan_unsaved())


def _plan_unsaved() -> director.CoveragePlan:
    p = director.CoveragePlan(beat_id="s001", beat_duration=12.0)
    p.coverage = [director.DirectorShot(id="s001.01", beat_id="s001", prompt="a",
                                        camera=Camera(move="static", duration=6.0))]
    return p


@pytest.mark.parametrize("mutate", [
    pytest.param(lambda p: setattr(p.coverage[0].camera, "duration", 7.0), id="duration"),
    pytest.param(lambda p: setattr(p.coverage[0], "motion_type", "ai_video"), id="motion_type"),
    pytest.param(lambda p: setattr(p.coverage[0], "prompt", "different"), id="prompt"),
    pytest.param(lambda p: setattr(p.coverage[0], "shot_size", "ecu"), id="framing"),
    pytest.param(lambda p: setattr(p.coverage[0].camera, "move", "push"), id="camera_move"),
    pytest.param(lambda p: p.coverage.pop(), id="shot_removed"),
    pytest.param(lambda p: setattr(p, "beat_duration", 20.0), id="beat_duration"),
])
def test_a_material_change_invalidates_the_approval(mutate):
    p = _plan_unsaved()
    p.coverage.append(director.DirectorShot(id="s001.02", beat_id="s001", prompt="b",
                                            camera=Camera(move="static", duration=6.0)))
    director.approve(p)
    assert director.approval_is_current(p)
    mutate(p)
    assert not director.approval_is_current(p)


@pytest.mark.parametrize("mutate", [
    pytest.param(lambda p: setattr(p.coverage[0], "chosen_variation", 2), id="take_selected"),
    pytest.param(lambda p: setattr(p.coverage[0], "draft_variations", ["a.png"]), id="drafts"),
    pytest.param(lambda p: setattr(p.coverage[0], "clip", "render/x.mp4"), id="clip"),
    pytest.param(lambda p: setattr(p.coverage[0], "estimated_cost", 9.99), id="cost"),
    pytest.param(lambda p: setattr(p.coverage[0], "reason", "rewritten rationale"), id="reason"),
])
def test_produced_state_does_not_invalidate_the_approval(mutate):
    """Selecting a take is Generate's business (§10), not a new Director decision.

    Too strict is its own failure: if choosing a different take sent the user
    back to Direct for re-approval, the approval gate would be trained out of
    them within a day.
    """
    p = _plan_unsaved()
    director.approve(p)
    mutate(p)
    assert director.approval_is_current(p)


# --- approval lifecycle -----------------------------------------------------------

def test_a_draft_carries_no_approval():
    assert not director.approval_is_current(_plan_unsaved())


def test_invalidation_returns_the_plan_to_draft_and_keeps_the_record():
    p = _plan_unsaved()
    director.approve(p)
    p.status = "locked"
    old_sig = p.approved_signature
    p.coverage[0].camera.duration = 9.0

    assert director.invalidate_approval(p) is True
    assert p.status == "draft", "a materially changed plan must not stay locked"
    assert p.approved_signature == ""
    assert p.approval_history[-1]["signature"] == old_sig
    assert p.approval_history[-1]["superseded_by"] == director.plan_signature(p)


def test_invalidating_an_unchanged_plan_is_a_no_op():
    p = _plan_unsaved()
    director.approve(p)
    p.status = "locked"
    assert director.invalidate_approval(p) is False
    assert p.status == "locked"


def test_re_approving_keeps_the_superseded_approval():
    p = _plan_unsaved()
    first = director.approve(p)
    p.coverage[0].prompt = "changed"
    second = director.approve(p)
    assert first != second
    assert p.approval_history[-1]["signature"] == first


# --- through the API --------------------------------------------------------------

def test_locking_records_an_approval_for_the_plan_it_was_given(studio):
    client, _ = studio
    _plan("draft")
    assert client.post("/api/director/lock/s001").status_code == 200
    saved = director.load_plan("s001")
    assert saved.approved_signature == director.plan_signature(saved)
    assert saved.approved_at and saved.approved_by == "human"


def test_the_scene_lock_records_approvals_too(studio):
    client, _ = studio
    _plan("draft")
    assert client.post("/api/director/lock_scene", json={"beats": ["s001"]}).status_code == 200
    assert director.approval_is_current(director.load_plan("s001"))


def test_unlocking_clears_the_approval(studio):
    client, _ = studio
    _plan("draft")
    client.post("/api/director/lock/s001")
    client.post("/api/director/lock/s001?locked=false")
    assert director.load_plan("s001").approved_signature == ""


def test_a_plan_edited_after_approval_cannot_compile(studio):
    """§11.5. The status still says locked; the signature says otherwise."""
    client, dispatched = studio
    _plan("draft")
    client.post("/api/director/lock/s001")

    # Edit the plan file directly, as a re-plan or a hand edit would.
    p = director.load_plan("s001")
    p.coverage[0].camera.duration = 5.0
    p.coverage[1].camera.duration = 7.0
    p.status = "locked"                      # the drift this guards against
    p.approved_signature = p.approved_signature or "x"
    director.save_plan(p)

    r = client.post("/api/director/compile/s001")
    assert r.status_code == 409
    assert r.json()["approval_drifted"] is True,         "refused, but for the wrong reason - this must be detected as drift"
    assert "changed after it was approved" in r.json()["error"]
    assert dispatched == [], "a plan nobody approved was sent to generation"


def test_an_approved_and_unchanged_plan_still_compiles(studio):
    """The gate has to let approved work through, or it proves nothing."""
    client, dispatched = studio
    _plan("draft")
    assert client.post("/api/director/lock/s001").status_code == 200
    assert client.post("/api/director/compile/s001").status_code == 200
    assert dispatched == ["director:s001"]


def test_selecting_a_take_after_approval_does_not_block_the_compile(studio):
    client, dispatched = studio
    _plan("draft")
    client.post("/api/director/lock/s001")
    p = director.load_plan("s001")
    p.coverage[0].chosen_variation = 1
    p.coverage[0].draft_variations = ["a.png", "b.png"]
    director.save_plan(p)
    assert client.post("/api/director/compile/s001").status_code == 200
    assert dispatched == ["director:s001"]


def test_the_plan_payload_states_approval_rather_than_implying_it(studio):
    client, _ = studio
    _plan("draft")
    client.post("/api/director/lock/s001")
    body = client.get("/api/director/plan/s001").json()
    assert body.get("approval_is_current") is True
    assert body.get("plan_signature")


# --- migration (idempotent, derives only what is authoritative) -------------------

def test_a_plan_locked_before_signatures_adopts_one_on_read(studio):
    """Derivable from authoritative state: it is locked, and this is that plan."""
    p = _plan("locked")
    raw = director.plan_path("s001").read_text(encoding="utf-8")
    assert "approved_signature" in raw

    loaded = director.load_plan("s001")
    assert director.approval_is_current(loaded)
    assert loaded.approved_by == "migrated:pre-signature-lock"


def test_migration_never_invents_an_approval_for_a_draft(studio):
    _plan("draft")
    loaded = director.load_plan("s001")
    assert loaded.approved_signature == ""
    assert not director.approval_is_current(loaded)


def test_migration_is_idempotent(studio):
    _plan("locked")
    first = director.load_plan("s001")
    director.save_plan(first)
    second = director.load_plan("s001")
    assert first.approved_signature == second.approved_signature


def test_a_never_approved_draft_is_refused_as_a_draft_not_as_drift(studio):
    """The two refusals must be distinguishable.

    "Lock it first" is misleading advice for a plan that WAS locked until
    someone edited it, and a test that only checks the status code cannot tell
    the two apart -- which is how a guard comes to fire for the wrong reason
    and still look green.
    """
    client, dispatched = studio
    _plan("draft")
    r = client.post("/api/director/compile/s001")
    assert r.status_code == 409
    assert r.json()["approval_drifted"] is False
    assert "is a draft" in r.json()["error"]
    assert dispatched == []
