"""A confirmed price must bind the plan it was quoted for, not just a beat id.

The studio's compile gate shows the human ``paid_shots`` and ``estimated_cost``
for the plan on screen and asks them to confirm the spend. The request that
follows carried only the beat id, and ``compile_director_coverage`` then
dispatched ``director.load_plan(beat_id)`` -- whatever was current at DISPATCH,
not what was quoted.

So: open the gate on a $1.85 plan, have a second tab (or a re-plan in another
view) replace and lock a $12.00 one, confirm -- and the newer plan compiled at
the newer price on consent given for the older one. The route accepted no plan
identity of any kind, so it structurally could not honour a quote. Refetching
before posting only narrows the window; the identity has to travel WITH the
request and be compared where the dispatch happens.

``director.plan_signature`` is that identity, and it is the right one: it covers
``motion_type``, ``backend`` and ``camera_duration`` -- everything cost is
derived from -- which is why ``estimated_cost`` is recorded in
``_NON_MATERIAL_SHOT_FIELDS`` as "already covered".

ASSERTION ORDER IS PART OF THE TEST, per the rule
``tests/test_concurrent_compile_dispatch.py`` states at length. The defect here
is a DISPATCH, so ``compiles.calls == 0`` runs first in every refusal test. The
status code is the polite half; the dispatch count is the half that costs money,
and a cheaper assertion failing first would hide it.
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

from backend import config, director  # noqa: E402
from backend.manifest import Camera, Shot, Storyboard  # noqa: E402

BEAT = "s001"


class Compiles:
    """A stand-in for `compile_coverage` that records what it was handed.

    It records the PLAN, not just a count: the defect is not "too many
    compiles", it is "the wrong plan compiled", and only the plan handed to the
    worker can show which one ran.
    """

    def __init__(self):
        self.plans: list[director.CoveragePlan] = []

    def __call__(self, plan, sb, render_dir, log=print):
        self.plans.append(plan)
        return Path(render_dir) / f"{plan.beat_id}.mp4"

    @property
    def calls(self) -> int:
        return len(self.plans)

    @property
    def dispatched_shot_counts(self) -> list[int]:
        return [len(p.coverage) for p in self.plans]


def _plan(shots: int, motion: str = "ai_video") -> director.CoveragePlan:
    """A locked, compilable plan of `shots` shots -- its cost is its size.

    `director.approve` binds a signature; `status` is the separate lifecycle
    fact the compile route checks, so both are set. A plan that is approved but
    still `draft` never reaches the binding under test.
    """
    plan = director.CoveragePlan(beat_id=BEAT, beat_duration=12.0, status="locked")
    plan.coverage = [
        director.DirectorShot(
            id=f"{BEAT}.{i + 1:02d}", beat_id=BEAT, motion_type=motion,
            prompt=f"shot {i + 1}",
            camera=Camera(move="static", duration=round(12.0 / shots, 3)),
        )
        for i in range(shots)
    ]
    director.approve(plan)
    director.save_plan(plan)
    return plan


@pytest.fixture
def studio(tmp_path, monkeypatch):
    """A real client, a real project, and a real (counted) dispatch path."""
    from fastapi.testclient import TestClient
    from backend import main as M

    manifest_path = tmp_path / "storyboard_manifest.json"
    monkeypatch.setattr(config, "MANIFEST_PATH", manifest_path)
    monkeypatch.setattr(M, "get_active_manifest_path", lambda: str(manifest_path))

    sb = Storyboard(title="T", script_locked=True, storyboard_approved=True,
                    shots=[Shot(scene_id=BEAT, narration="n", prompt="p",
                                camera=Camera(move="static", duration=12.0))])
    monkeypatch.setattr(M, "get_current_project", lambda: sb)

    compiles = Compiles()
    monkeypatch.setattr(M.director, "compile_coverage", compiles)
    yield TestClient(M.app, raise_server_exceptions=False), compiles


def _settle():
    """Join every job worker, so `calls` is final rather than not-yet-run.

    `start_job` returns as soon as the thread is spawned, so a dispatch is in
    flight when the response arrives. Without this a test asserting `calls == 1`
    could pass on a race, and -- far worse here -- a test asserting `calls == 0`
    could pass against a compile that simply had not got there yet.
    """
    import threading
    me = threading.current_thread()
    for t in threading.enumerate():
        if t is not me and t.name.startswith("job-"):
            t.join(timeout=10)


# --- the defect ---------------------------------------------------------------

def test_a_plan_replaced_after_the_quote_is_not_compiled(studio):
    """The HIGH, at the level it was reproduced.

    The human is quoted for the 2-shot plan. A second tab replaces and locks a
    9-shot one. The confirmation must not spend on the 9-shot plan.
    """
    client, compiles = studio

    quoted = _plan(2)
    quoted_signature = director.plan_signature(quoted)

    # Somewhere else entirely: a new plan for the same beat, locked and valid.
    _plan(9)

    r = client.post(f"/api/director/compile/{BEAT}",
                    params={"plan_signature": quoted_signature})
    _settle()

    # THE DEFECT, and it goes first. Under the regression this is [9]: nine
    # shots' worth of paid generation on consent given for two. Asserting the
    # status code first would report "expected 409, got 200" and never once
    # evaluate what was actually sent to the renderer.
    assert compiles.plans == [], (
        f"{compiles.calls} compile(s) dispatched with shot counts "
        f"{compiles.dispatched_shot_counts}; the human confirmed a 2-shot plan, "
        f"so anything dispatched here was bought without consent")

    # Only then, how it was reported.
    assert r.status_code == 409, r.text
    body = r.json()
    assert body["ok"] is False
    assert body["signature_mismatch"] is True
    assert "changed after you were quoted a price" in body["error"]
    assert "nothing was charged" in body["error"]


def test_the_refusal_hands_back_the_signature_needed_to_re_quote(studio):
    """A refusal the human cannot act on is half a refusal.

    They have to be re-quoted, so the reply names the plan that is actually
    there now -- and says which one they had agreed to, so the two can be told
    apart in a log after the fact.
    """
    client, compiles = studio

    quoted_signature = director.plan_signature(_plan(2))
    replacement = _plan(9)

    r = client.post(f"/api/director/compile/{BEAT}",
                    params={"plan_signature": quoted_signature})
    _settle()

    assert compiles.plans == []
    body = r.json()
    assert body["plan_signature"] == director.plan_signature(replacement)
    assert body["quoted_signature"] == quoted_signature
    assert body["plan_signature"] != body["quoted_signature"]


def test_an_edit_that_does_not_change_cost_still_invalidates_the_quote(studio):
    """The signature is the identity of the plan, not a price comparison.

    A same-shape edit -- a rewritten prompt, an angle change -- leaves
    `estimated_cost` untouched and still produces different output. Comparing
    prices rather than identities would wave this through, and the human would
    get a different film than the one they approved at the same price.
    """
    client, compiles = studio

    quoted = _plan(2)
    quoted_signature = director.plan_signature(quoted)

    edited = director.load_plan(BEAT)
    edited.coverage[0].prompt = "a completely different image"
    director.approve(edited)
    director.save_plan(edited)

    r = client.post(f"/api/director/compile/{BEAT}",
                    params={"plan_signature": quoted_signature})
    _settle()

    assert compiles.plans == []
    assert r.status_code == 409
    assert r.json()["signature_mismatch"] is True


# --- the other direction, or the tests above pass against `return 409` --------

def test_the_quoted_plan_compiles(studio):
    """A matching signature must not be an obstacle. This is the common path."""
    client, compiles = studio

    quoted_signature = director.plan_signature(_plan(2))

    r = client.post(f"/api/director/compile/{BEAT}",
                    params={"plan_signature": quoted_signature})
    _settle()

    # Dispatch first here too: a binding that refuses everything answers 409,
    # and asserting the status first would report a code mismatch rather than
    # the fact that matters, which is that the approved plan never ran.
    assert compiles.dispatched_shot_counts == [2], (
        "the plan the human confirmed was not compiled")
    assert r.status_code == 200, r.text
    assert r.json()["started"] is True


def test_an_unsigned_compile_is_refused(studio):
    """INVERTED. This test used to assert the opposite, and that was the defect.

    It read `test_a_caller_that_quoted_no_price_is_unaffected`, and it asserted
    `dispatched_shot_counts == [2]` and a 200 -- a test positively expecting an
    unsigned request to buy paid video. That is worse than an untested path: it
    is the suite asserting that the boundary is optional, so closing it would
    have looked like a regression and been reverted.

    The reasoning it was written on was that a caller which quoted no price has
    no consent to honour, so there is nothing to compare and nothing at risk.
    The first half is true and the conclusion is backwards. A caller that never
    said what it agreed to has not agreed to anything, and `if plan_signature
    and ...` turned that into an opt-out: omit the parameter, skip the check.
    """
    client, compiles = studio

    _plan(2)

    r = client.post(f"/api/director/compile/{BEAT}")
    _settle()

    # Dispatch first. Under the old behaviour this is [2] -- and with a plan
    # swapped underneath, whatever the swap put there.
    assert compiles.plans == [], (
        f"{compiles.calls} unsigned compile(s) dispatched with shot counts "
        f"{compiles.dispatched_shot_counts}; nobody said what they were paying for")

    assert r.status_code == 400, r.text
    body = r.json()
    assert body["signature_missing"] is True
    # Its own message. "you did not say what you agreed to" is different advice
    # from "what you agreed to has changed", so it must not be folded into the
    # mismatch refusal.
    assert "did not say which plan it was approving" in body["error"]
    assert "nothing was charged" in body["error"]
    assert "signature_mismatch" not in body


def test_an_empty_signature_is_refused_exactly_like_a_missing_one(studio):
    """INVERTED, and it was the more dangerous of the two.

    `?plan_signature=` looks like a caller that tried. The old test asserted it
    dispatched, on the reasoning that empty is the same as absent -- which is
    true, and is precisely why both must be refused rather than both waved
    through.
    """
    client, compiles = studio

    _plan(2)

    r = client.post(f"/api/director/compile/{BEAT}", params={"plan_signature": ""})
    _settle()

    assert compiles.plans == []
    assert r.status_code == 400, r.text
    assert r.json()["signature_missing"] is True


def test_a_malformed_signature_is_refused_and_not_reported_as_a_mismatch(studio):
    """Not a signature at all, so there is nothing to have changed.

    Sixteen lowercase hex or it is not one. A caller sending "yes", or a
    truncated hash, or a plan_id does not know what a signature is -- the same
    reasoning `approval_is_explicit` refuses "true" on. Reported as unsigned
    rather than as a mismatch, because telling this caller their plan changed
    would send them to re-approve a plan that is fine.
    """
    client, compiles = studio

    _plan(2)

    for bogus in ("yes", "ABCDEF0123456789", "ab12cd34", "ab12cd34ef5678901"):
        compiles.plans.clear()
        r = client.post(f"/api/director/compile/{BEAT}",
                        params={"plan_signature": bogus})
        _settle()

        assert compiles.plans == [], f"{bogus!r} dispatched a compile"
        assert r.status_code == 400, f"{bogus!r}: {r.text}"
        body = r.json()
        assert body["signature_missing"] is True, f"{bogus!r}: {body}"
        assert "not a plan signature" in body["error"], f"{bogus!r}: {body}"


def test_the_unsigned_refusal_does_not_hand_back_the_signature(studio):
    """Nothing to re-quote here, so nothing is offered.

    The mismatch refusal returns `plan_signature`, because a human at the screen
    has to be shown what the plan is now. This one has no quote to correct, and
    echoing the value back to a caller that never asked a human anything reads
    as an invitation to send it straight back. It is not a secret -- the scene
    endpoint carries `approved_signature` -- but it is not this route's job to
    hand out the answer to the question it just refused.
    """
    client, compiles = studio

    _plan(2)

    body = client.post(f"/api/director/compile/{BEAT}").json()

    assert compiles.plans == []
    assert "plan_signature" not in body


def test_the_draft_gate_still_answers_first_for_an_unsigned_caller(studio):
    """Order, and it is the reason the signature check sits last.

    "lock it first" is better advice than "sign your request" for a plan that
    was never approved -- and a plan that was never approved has no signature to
    send, so demanding one first would be advice nobody could act on. Nothing
    dispatches either way, which is what makes the ordering a matter of what the
    human is told rather than of whether the boundary holds.
    """
    client, compiles = studio

    plan = _plan(2)
    plan.status = "draft"
    plan.approved_signature = ""
    director.save_plan(plan)

    r = client.post(f"/api/director/compile/{BEAT}")
    _settle()

    assert compiles.plans == []
    body = r.json()
    assert body.get("approval_drifted") is False, body
    assert "lock it first" in body["error"]
    assert "signature_missing" not in body


def test_consent_is_checked_before_the_plan_s_own_state(studio):
    """A plan they have never seen must not be described to them.

    If the replacement is a draft, "lock it first" is advice about a plan the
    human did not write and has not read. What they need to know is that the
    thing they agreed to pay for is gone.
    """
    client, compiles = studio

    quoted_signature = director.plan_signature(_plan(2))

    replacement = director.load_plan(BEAT)
    replacement.coverage.append(
        director.DirectorShot(id=f"{BEAT}.03", beat_id=BEAT, motion_type="ai_video",
                              prompt="c", camera=Camera(move="static", duration=4.0)))
    replacement.status = "draft"
    replacement.approved_signature = ""
    director.save_plan(replacement)

    r = client.post(f"/api/director/compile/{BEAT}",
                    params={"plan_signature": quoted_signature})
    _settle()

    assert compiles.plans == []
    body = r.json()
    assert body.get("signature_mismatch") is True, (
        f"the draft check answered first: {body.get('error')}")
    assert "approval_drifted" not in body
