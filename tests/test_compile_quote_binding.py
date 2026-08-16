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


def test_a_caller_that_quoted_no_price_is_unaffected(studio):
    """Back-compatibility, stated as a decision rather than left implicit.

    The CLI and the existing suite post without a signature. They quoted no
    price, so there is no consent to honour and nothing to compare against;
    inventing a binding for them would refuse work that was never at risk. The
    lock and staleness gates still apply to them exactly as before.
    """
    client, compiles = studio

    _plan(2)

    r = client.post(f"/api/director/compile/{BEAT}")
    _settle()

    assert compiles.dispatched_shot_counts == [2]
    assert r.status_code == 200, r.text


def test_an_empty_signature_is_not_treated_as_a_mismatch(studio):
    """`?plan_signature=` is the same as not sending one, not a wrong answer."""
    client, compiles = studio

    _plan(2)

    r = client.post(f"/api/director/compile/{BEAT}", params={"plan_signature": ""})
    _settle()

    assert compiles.dispatched_shot_counts == [2]
    assert r.status_code == 200, r.text


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
