"""Script changes must not silently corrupt a plan (contract §4, §10).

`director.validate` already refuses a plan whose beat has been re-timed. The gap
this covers is the quiet one: a narration line rewritten to the SAME length
moves past every existing check, while every prompt in the plan still describes
the old line. Nothing raised, and the beat compiled.

The other half of §4 is what must NOT happen — generated media is preserved and
unrelated beats are left alone. A staleness rule that deleted work, or that
marked the whole episode stale because one line changed, would be its own bug.
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


ORIGINAL = "The manananggal severs itself at the waist and leaves its legs standing."
SAME_LENGTH = "The aswang divides itself below the ribs and abandons the lower half."


@pytest.fixture
def studio(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from backend import main as M

    monkeypatch.setattr(director.config, "MANIFEST_PATH",
                        tmp_path / "storyboard_manifest.json")
    dispatched: list[str] = []
    monkeypatch.setattr(M, "start_job",
                        lambda name, fn, *a, **k: (dispatched.append(name), True)[1])
    sb = Storyboard(
        title="T", script_locked=True, storyboard_approved=True,
        shots=[Shot(scene_id="s001", narration=ORIGINAL, prompt="p",
                    camera=Camera(move="static", duration=12.0)),
               Shot(scene_id="s002", narration="An unrelated beat.", prompt="p",
                    camera=Camera(move="static", duration=8.0))],
    )
    monkeypatch.setattr(M, "get_current_project", lambda: sb)
    return TestClient(M.app, raise_server_exceptions=False), dispatched, sb


def _plan(beat_id="s001", seconds=12.0) -> director.CoveragePlan:
    p = director.CoveragePlan(beat_id=beat_id, beat_duration=seconds, status="draft")
    half = seconds / 2
    p.coverage = [
        director.DirectorShot(id=f"{beat_id}.01", beat_id=beat_id, motion_type="parallax",
                              prompt="a", camera=Camera(move="static", duration=half)),
        director.DirectorShot(id=f"{beat_id}.02", beat_id=beat_id, motion_type="parallax",
                              prompt="b", camera=Camera(move="static", duration=half)),
    ]
    director.save_plan(p)
    return p


# --- the gap: a rewrite at the same length --------------------------------------

def test_a_rewritten_line_of_the_same_length_is_detected(studio):
    client, dispatched, sb = studio
    _plan()
    assert client.post("/api/director/lock/s001").status_code == 200

    sb.shots[0].narration = SAME_LENGTH        # same duration, different words
    r = client.post("/api/director/compile/s001")
    assert r.status_code == 409
    assert r.json()["stale"]["kind"] == "narration"
    assert dispatched == [], "compiled prompts written for a line that changed"


def test_an_unchanged_beat_still_compiles(studio):
    """The check has to let real work through, or it proves nothing."""
    client, dispatched, _ = studio
    _plan()
    client.post("/api/director/lock/s001")
    assert client.post("/api/director/compile/s001").status_code == 200
    assert dispatched == ["director:s001"]


def test_whitespace_only_edits_are_not_a_rewrite(studio):
    """Reflowing a line is not a script change; flagging it would train the
    warning out of the user."""
    client, dispatched, sb = studio
    _plan()
    client.post("/api/director/lock/s001")
    sb.shots[0].narration = "  " + ORIGINAL.replace(" ", "  ") + chr(10)
    assert client.post("/api/director/compile/s001").status_code == 200


def test_re_locking_accepts_the_new_line(studio):
    """Re-approval is how a human says they have read the rewrite."""
    client, _, sb = studio
    _plan()
    client.post("/api/director/lock/s001")
    sb.shots[0].narration = SAME_LENGTH

    assert client.post("/api/director/compile/s001").status_code == 409
    assert client.post("/api/director/lock/s001").status_code == 200
    assert client.post("/api/director/compile/s001").status_code == 200


# --- what must NOT happen (§4) ----------------------------------------------------

def test_a_script_change_does_not_touch_unrelated_beats(studio):
    """'Do not silently remap unrelated assets' -- s002 is nobody's business here."""
    client, _, sb = studio
    _plan("s001", 12.0)
    _plan("s002", 8.0)
    client.post("/api/director/lock/s001")
    client.post("/api/director/lock/s002")

    sb.shots[0].narration = SAME_LENGTH

    assert client.post("/api/director/compile/s001").status_code == 409
    assert client.post("/api/director/compile/s002").status_code == 200
    assert director.load_plan("s002").status == "locked"


def test_generated_media_survives_a_script_change(studio):
    """§4: preserve existing generated media. Staleness blocks, it does not delete."""
    client, _, sb = studio
    p = _plan()
    p.coverage[0].clip = "render/s001/s001.01.mp4"
    p.coverage[0].draft_variations = ["a.png", "b.png"]
    p.coverage[0].chosen_variation = 1
    p.coverage[0].paid_clip = "render/s001/s001.01.mp4"
    director.save_plan(p)
    client.post("/api/director/lock/s001")

    sb.shots[0].narration = SAME_LENGTH
    client.post("/api/director/compile/s001")

    after = director.load_plan("s001")
    assert after.coverage[0].clip == "render/s001/s001.01.mp4"
    assert after.coverage[0].draft_variations == ["a.png", "b.png"]
    assert after.coverage[0].chosen_variation == 1
    assert after.coverage[0].paid_clip == "render/s001/s001.01.mp4"


# --- the signature itself ----------------------------------------------------------

def _beat(narration=ORIGINAL, seconds=12.0) -> Shot:
    return Shot(scene_id="s001", narration=narration,
                camera=Camera(move="static", duration=seconds))


def test_the_beat_signature_covers_the_text():
    assert director.beat_signature(_beat()) != director.beat_signature(_beat(SAME_LENGTH))


def test_the_beat_signature_covers_the_duration():
    assert director.beat_signature(_beat()) != director.beat_signature(_beat(seconds=13.0))


def test_the_beat_signature_ignores_whitespace():
    assert director.beat_signature(_beat()) == director.beat_signature(
        _beat("  " + ORIGINAL.replace(" ", "   ")))


def test_retiming_and_rewriting_together_says_so():
    p = director.CoveragePlan(beat_id="s001", beat_duration=12.0)
    p.beat_signature = director.beat_signature(_beat())
    assert director.beat_staleness(p, _beat(SAME_LENGTH, 20.0))["kind"] == "both"


def test_a_plan_with_no_baseline_is_not_called_stale():
    """It predates the check and the old narration is not recoverable. Guessing
    would flag every legacy plan on the first load after deploy."""
    p = director.CoveragePlan(beat_id="s001", beat_duration=12.0)
    assert p.beat_signature == ""
    assert director.beat_staleness(p, _beat(SAME_LENGTH)) is None


def test_locking_a_legacy_plan_gives_it_a_baseline(studio):
    client, _, sb = studio
    _plan()
    assert director.load_plan("s001").beat_signature == ""
    client.post("/api/director/lock/s001")
    assert director.load_plan("s001").beat_signature == director.beat_signature(sb.shots[0])


def test_the_plan_payload_states_staleness(studio):
    client, _, sb = studio
    _plan()
    client.post("/api/director/lock/s001")
    sb.shots[0].narration = SAME_LENGTH
    body = client.get("/api/director/plan/s001").json()
    assert body["stale"]["kind"] == "narration"
