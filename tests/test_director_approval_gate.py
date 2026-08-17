"""The Director approval boundary: DRAFT -> CRITIC -> HUMAN APPROVAL -> GENERATE.

Closes the two coverage gaps the Pass-A adversarial audit found (TG-01, TG-02).
Both were invisible to the previous suite because the existing tests asserted on
*response text* while the guard's real job is to stop work being **dispatched**.
A guard that returns a polite error and starts the job anyway reads as passing.

So these tests assert on the dispatch itself: `start_job` is patched to record
calls and never run anything, and the assertions are about whether generation
was handed off. Each one is mutation-checked in the sense that it targets the
branch, not the message.
"""

from __future__ import annotations

import json
import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
for _m in ("anthropic", "fal_client", "elevenlabs"):
    sys.modules.setdefault(_m, types.ModuleType(_m))

pytest.importorskip("fastapi.testclient")
from signed_compile import compile_beat  # noqa: E402

from backend import director  # noqa: E402
from backend.manifest import Camera, Shot, Storyboard  # noqa: E402


WARNING = {
    "beat_id": "s001",
    "shot_id": "s001.01",
    "kind": "repetition",
    "detail": "Three consecutive medium shots on the same axis.",
    "suggestion": "Vary one of them.",
}


@pytest.fixture
def studio(tmp_path, monkeypatch):
    """A client plus the list of jobs that were actually dispatched."""
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


def _plan(status: str = "draft", warnings: list | None = None) -> director.CoveragePlan:
    p = director.CoveragePlan(beat_id="s001", beat_duration=12.0, status=status)
    p.coverage = [
        director.DirectorShot(id="s001.01", beat_id="s001", motion_type="parallax",
                              prompt="a", camera=Camera(move="static", duration=6.0)),
        director.DirectorShot(id="s001.02", beat_id="s001", motion_type="parallax",
                              prompt="b", camera=Camera(move="static", duration=6.0)),
    ]
    p.warnings = director.normalize_warnings(warnings or [])
    director.save_plan(p)
    return p


# Assertion order, and why it is the way it is.
#
# These tests exist because a guard that returns a polite error and starts the
# job anyway reads as passing, so what they assert on is DISPATCH. But they were
# asserting the status code FIRST, and under every mutation that opens the gate
# the status moves too -- so the status assertion failed, pytest stopped, and
# the line that says work was handed to generation never ran. The table went red
# having demonstrated a number, not a spend. Mutation testing surfaced it
# (scratch/mutate_pass_a.py reported five of these as "the test died on a
# cheaper assertion first"), and the fix is ordering: dispatch, then status.

# --- TG-01: a draft plan can never reach generation -----------------------------

def test_a_draft_plan_is_refused_at_compile(studio):
    client, dispatched = studio
    _plan("draft")
    r = client.post("/api/director/compile/s001")
    assert dispatched == [], "a draft plan was handed to generation"
    assert r.status_code == 409


def test_the_force_flag_no_longer_bypasses_approval(studio):
    """A-01. This was HTTP 200 + started:true against an unapproved plan."""
    client, dispatched = studio
    _plan("draft")
    r = client.post("/api/director/compile/s001?force=true")
    assert dispatched == [], "force=true sent an unapproved plan into generation"
    assert r.status_code == 409


@pytest.mark.parametrize("force", ["true", "false", "1", "TRUE"])
def test_no_spelling_of_force_gets_a_draft_through(studio, force):
    """The flag is gone, so unknown query parameters must simply be ignored."""
    client, dispatched = studio
    _plan("draft")
    r = client.post(f"/api/director/compile/s001?force={force}")
    assert dispatched == [], f"force={force} sent an unapproved plan into generation"
    assert r.status_code == 409


def test_a_locked_plan_does_compile(studio):
    """The gate has to still let approved work through, or it proves nothing."""
    client, dispatched = studio
    _plan("locked")
    r = compile_beat(client, "s001")
    assert r.status_code == 200
    assert r.json()["started"] is True
    assert dispatched == ["director:s001"]


def test_a_locked_plan_with_an_undecided_warning_still_cannot_compile(studio):
    """Defence in depth for plans locked before the review rule existed."""
    client, dispatched = studio
    _plan("locked", [WARNING])
    r = client.post("/api/director/compile/s001")
    assert dispatched == [], \
        "a plan locked before the review rule was handed to generation"
    assert r.status_code == 409


# --- TG-02: unresolved critic findings cannot be silently bulk-approved ---------

def test_scene_lock_refuses_a_plan_with_an_undecided_warning(studio):
    """A-02. 'Approve Scene Plan' used to lock straight past every warning."""
    client, _ = studio
    _plan("draft", [WARNING])
    r = client.post("/api/director/lock_scene", json={"beats": ["s001"]})
    assert director.load_plan("s001").status == "draft", "locked despite the warning"
    assert r.status_code == 400


def test_beat_lock_refuses_a_plan_with_an_undecided_warning(studio):
    """The single-beat route is a second way in and needs the same rule."""
    client, _ = studio
    _plan("draft", [WARNING])
    r = client.post("/api/director/lock/s001")
    assert director.load_plan("s001").status == "draft", \
        "the single-beat route locked despite the warning"
    assert r.status_code == 400


def test_a_clean_plan_locks_normally(studio):
    client, _ = studio
    _plan("draft", [])
    assert client.post("/api/director/lock_scene", json={"beats": ["s001"]}).status_code == 200
    assert director.load_plan("s001").status == "locked"


def test_locking_is_allowed_once_every_warning_has_a_decision(studio):
    client, _ = studio
    p = _plan("draft", [WARNING])
    wid = p.warnings[0]["id"]

    r = client.post(f"/api/director/warning/s001/{wid}", json={"decision": "accepted",
                                                              "note": "deliberate"})
    assert r.status_code == 200
    assert r.json()["unresolved"] == 0

    assert client.post("/api/director/lock_scene", json={"beats": ["s001"]}).status_code == 200
    assert director.load_plan("s001").status == "locked"


def test_unlocking_back_to_draft_is_never_blocked_by_warnings(studio):
    """Sending a plan back for more work is not an approval."""
    client, _ = studio
    _plan("locked", [WARNING])
    assert client.post("/api/director/lock/s001?locked=false").status_code == 200
    assert director.load_plan("s001").status == "draft"


# --- A-03: a decision is durable, and dismissal is not a decision ----------------

def test_a_decision_survives_a_reload(studio):
    """The UI used to filter the warning out of React state and nothing else."""
    client, _ = studio
    p = _plan("draft", [WARNING])
    wid = p.warnings[0]["id"]
    client.post(f"/api/director/warning/s001/{wid}", json={"decision": "resolved"})

    reloaded = director.load_plan("s001")
    assert reloaded.warning_dispositions[wid]["decision"] == "resolved"
    assert director.unresolved_warnings(reloaded) == []
    assert len(reloaded.warnings) == 1, "the finding itself must be retained"


def test_clearing_a_decision_makes_the_warning_block_again(studio):
    client, _ = studio
    p = _plan("draft", [WARNING])
    wid = p.warnings[0]["id"]
    client.post(f"/api/director/warning/s001/{wid}", json={"decision": "accepted"})
    client.post(f"/api/director/warning/s001/{wid}", json={"decision": ""})
    assert client.post("/api/director/lock/s001").status_code == 400


def test_a_decision_about_an_unknown_warning_is_refused(studio):
    client, _ = studio
    _plan("draft", [WARNING])
    assert client.post("/api/director/warning/s001/deadbeef",
                       json={"decision": "resolved"}).status_code == 400


def test_an_unknown_decision_value_is_refused(studio):
    client, _ = studio
    p = _plan("draft", [WARNING])
    wid = p.warnings[0]["id"]
    r = client.post(f"/api/director/warning/s001/{wid}", json={"decision": "whatever"})
    assert r.status_code == 400
    assert director.unresolved_warnings(director.load_plan("s001"))


# --- warning identity ------------------------------------------------------------

def test_a_warning_id_is_stable_across_recomputation():
    """A decision must survive a re-critique that reports the same finding."""
    assert director.warning_id(WARNING) == director.warning_id(dict(WARNING))


def test_a_changed_warning_gets_a_new_id():
    """Resolving one finding must never silently clear a different one."""
    changed = dict(WARNING, detail="Something else entirely.")
    assert director.warning_id(changed) != director.warning_id(WARNING)


def test_normalizing_warnings_is_idempotent():
    once = director.normalize_warnings([WARNING])
    assert director.normalize_warnings(once) == once


def test_legacy_string_warnings_survive_migration():
    """Older plans stored bare strings; they must not lose their text."""
    out = director.normalize_warnings(["coverage is thin"])
    assert out[0]["detail"] == "coverage is thin"
    assert out[0]["id"]


def test_dispositions_round_trip_through_the_plan_file(studio):
    client, _ = studio
    p = _plan("draft", [WARNING])
    wid = p.warnings[0]["id"]
    client.post(f"/api/director/warning/s001/{wid}", json={"decision": "accepted",
                                                           "note": "on purpose"})
    raw = json.loads(director.plan_path("s001").read_text(encoding="utf-8"))
    assert raw["warning_dispositions"][wid]["note"] == "on purpose"


def test_a_suggestionless_warning_can_still_be_decided(studio):
    """Otherwise its scene could never be locked.

    The problem drawer only rendered an action for warnings that carried a
    suggestion. Once locking started requiring a decision on every finding, a
    suggestion-less warning would have been undecidable through the UI and its
    scene permanently unlockable.
    """
    client, _ = studio
    bare = {"beat_id": "s001", "shot_id": "", "kind": "timing_mismatch",
            "detail": "Coverage totals 11.40s against a 12.00s beat."}
    p = _plan("draft", [bare])
    wid = p.warnings[0]["id"]
    assert client.post(f"/api/director/warning/s001/{wid}",
                       json={"decision": "accepted"}).status_code == 200
    assert client.post("/api/director/lock/s001").status_code == 200


def test_a_recritique_that_repeats_a_finding_keeps_its_decision(studio, monkeypatch):
    """A decision must survive Re-check, or review becomes Sisyphean."""
    from backend import main as M
    client, _ = studio
    p = _plan("draft", [WARNING])
    wid = p.warnings[0]["id"]
    client.post(f"/api/director/warning/s001/{wid}", json={"decision": "accepted"})

    monkeypatch.setattr(M.planner, "critique", lambda sb, beats, **kw: [dict(WARNING)])
    monkeypatch.setattr(M.planner, "scene_summary", lambda ids: {"estimated_cost": 0.0})
    assert client.post("/api/director/critique", json={"beats": ["s001"]}).status_code == 200

    after = director.load_plan("s001")
    assert director.unresolved_warnings(after) == [], "the decision was lost on re-check"


def test_a_recritique_that_changes_a_finding_drops_the_stale_decision(studio, monkeypatch):
    """The opposite risk: an old 'accepted' must not cover a new problem."""
    from backend import main as M
    client, _ = studio
    p = _plan("draft", [WARNING])
    client.post(f"/api/director/warning/s001/{p.warnings[0]['id']}",
                json={"decision": "accepted"})

    changed = dict(WARNING, detail="Now the protagonist is in every single shot.")
    monkeypatch.setattr(M.planner, "critique", lambda sb, beats, **kw: [changed])
    monkeypatch.setattr(M.planner, "scene_summary", lambda ids: {"estimated_cost": 0.0})
    client.post("/api/director/critique", json={"beats": ["s001"]})

    after = director.load_plan("s001")
    assert len(director.unresolved_warnings(after)) == 1
    assert after.warning_dispositions == {}, "a stale decision covered a new finding"


# --- A-04: identity is derived, never supplied ----------------------------------

def test_a_supplied_warning_id_cannot_carry_a_decision_onto_a_changed_finding(studio):
    """A-04. Trusting an incoming id lets a NEW problem arrive pre-approved.

    Two materially different findings that both claim the same id must not
    share a disposition. Identity is a property of what a warning says, not of
    who handed it over.
    """
    client, _ = studio
    old = dict(WARNING, id="critic-fixed-id")
    p = _plan("draft", [old])
    wid = p.warnings[0]["id"]
    assert wid != "critic-fixed-id", "a caller-supplied id became the identity"
    client.post(f"/api/director/warning/s001/{wid}", json={"decision": "accepted"})
    assert director.unresolved_warnings(director.load_plan("s001")) == []

    # The critic now reports something else entirely under the same id.
    changed = {"id": "critic-fixed-id", "beat_id": "s001", "shot_id": "s001.09",
               "kind": "identity_risk", "detail": "The protagonist's face is unreadable."}
    p2 = director.load_plan("s001")
    p2.warnings = director.normalize_warnings([changed])
    director.save_plan(p2)

    assert len(director.unresolved_warnings(director.load_plan("s001"))) == 1, \
        "a changed finding inherited the decision made about a different one"
    assert client.post("/api/director/lock/s001").status_code == 400


def test_a_supplied_id_is_kept_for_tracing_but_is_not_the_key(studio):
    client, _ = studio
    p = _plan("draft", [dict(WARNING, id="critic-fixed-id")])
    w = p.warnings[0]
    assert w["source_id"] == "critic-fixed-id"
    assert w["id"] == director.warning_id(WARNING)


def test_normalizing_is_idempotent_even_with_a_supplied_id():
    once = director.normalize_warnings([dict(WARNING, id="critic-fixed-id")])
    assert director.normalize_warnings(once) == once


def test_two_findings_that_differ_only_by_target_get_different_ids():
    a = dict(WARNING, shot_id="s001.01")
    b = dict(WARNING, shot_id="s001.02")
    assert director.warning_id(a) != director.warning_id(b)
