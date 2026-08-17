"""TG-S4-04: two compile requests racing must produce exactly ONE dispatch.

The suite already had domain-level crash tests (`tests/test_generation_lineage.py`)
and job-registry tests (`tests/test_project_isolation.py`). Neither issued
concurrent **HTTP** compiles, so nothing covered the guard as the product
actually presents it: two clicks, two tabs, a client retrying a request it
thought had timed out.

The adversarial re-audit reproduced the safeguard twice and recorded the shape
it wants pinned:

    first request:  200
    second request: 409
    compile calls:  1

All three facts are asserted here, and the third is the one that matters. A test
that checks only the 409 would still pass against a `start_job` that refused the
second caller *after* handing its work to a thread -- the status code is the
polite half, the dispatch count is the half that costs money. `compile_coverage`
is where a beat's ai_video shots reach fal, so counting its calls counts the
money the guard exists to save.

Nothing about the dispatch path is faked: real `start_job`, real worker threads,
real requests through the ASGI app and both middlewares. Only `compile_coverage`
itself is replaced, because it is both the thing being counted and the thing
that would otherwise call a paid API.

ASSERTION ORDER IS PART OF THE TEST, and this file is where that rule was
learned the expensive way. These tests originally asserted the 200/409 split
first and the dispatch count after it. Under the regression they exist for the
codes are [200, 200], so the status assertion failed, the run aborted, and the
dispatch count -- the money-relevant half, the entire point of TG-S4-04 -- was
never evaluated once. The test reproduced the exact flaw its own docstring
warns about, two paragraphs up.

So: within a test, the assertion that demonstrates the defect runs FIRST, and
anything cheaper or more cosmetic runs after it. Read every value you intend to
assert on before asserting on any of them, and never index one result by
another (`bodies[codes.index(409)]` raises ValueError under the very regression
being guarded, which is an error, not a failure, and reports nothing).
"""

from __future__ import annotations

import sys
import threading
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
for _m in ("anthropic", "fal_client", "elevenlabs"):
    sys.modules.setdefault(_m, types.ModuleType(_m))

pytest.importorskip("fastapi.testclient")
from signed_compile import compile_beat, signature_for  # noqa: E402

from backend import config, director  # noqa: E402
from backend.manifest import Camera, Shot, Storyboard  # noqa: E402

BEAT = "s001"


class Compiles:
    """A stand-in for `compile_coverage` that counts and can be held open.

    Held open on purpose: the guard being tested asks "is a compile for this
    beat already running?", so the first job has to still be running when the
    second request arrives. Releasing it only after both responses are in is
    what makes the race deterministic without weakening it -- the two requests
    still contend for the registry lock, and which one wins is not fixed.
    """

    def __init__(self):
        self.calls = 0
        self._lock = threading.Lock()
        self.entered = threading.Event()
        self.proceed = threading.Event()

    def __call__(self, plan, sb, render_dir, log=print):
        with self._lock:
            self.calls += 1
        self.entered.set()
        self.proceed.wait(timeout=10)
        return Path(render_dir) / f"{plan.beat_id}.mp4"

    def release_and_settle(self):
        """Let every dispatched compile finish, then wait for its thread.

        A second dispatch is already a live thread by the time its response
        returns -- `start_job` starts the thread before it returns -- so joining
        every job thread here is what makes `calls` final rather than merely
        not-yet-incremented.
        """
        self.proceed.set()
        me = threading.current_thread()
        for t in threading.enumerate():
            if t is not me and t.name.startswith("job-"):
                t.join(timeout=10)


@pytest.fixture
def studio(tmp_path, monkeypatch):
    """A real client over a real project with one locked, compilable plan."""
    from fastapi.testclient import TestClient
    from backend import main as M

    manifest_path = tmp_path / "storyboard_manifest.json"
    monkeypatch.setattr(config, "MANIFEST_PATH", manifest_path)
    monkeypatch.setattr(M, "get_active_manifest_path", lambda: str(manifest_path))

    sb = Storyboard(title="T", script_locked=True, storyboard_approved=True,
                    shots=[Shot(scene_id=BEAT, narration="n", prompt="p",
                                camera=Camera(move="static", duration=12.0))])
    monkeypatch.setattr(M, "get_current_project", lambda: sb)

    plan = director.CoveragePlan(beat_id=BEAT, beat_duration=12.0, status="locked")
    plan.coverage = [
        director.DirectorShot(id=f"{BEAT}.01", beat_id=BEAT, motion_type="ai_video",
                              prompt="a", camera=Camera(move="static", duration=6.0)),
        director.DirectorShot(id=f"{BEAT}.02", beat_id=BEAT, motion_type="parallax",
                              prompt="b", camera=Camera(move="static", duration=6.0)),
    ]
    director.approve(plan)
    director.save_plan(plan)

    compiles = Compiles()
    monkeypatch.setattr(M.director, "compile_coverage", compiles)
    try:
        yield TestClient(M.app, raise_server_exceptions=False), compiles
    finally:
        # Never leave a worker thread parked on `proceed` if an assertion blew
        # up before the release: it holds the beat's job key, and the next test
        # to use this beat would see a stale "already running".
        compiles.release_and_settle()


def _race(client, n=2):
    """Fire `n` compiles for the same beat as simultaneously as threads allow."""
    # Signed once, outside the race, and every caller sends the SAME signature.
    # That is what two tabs on one plan actually do, and it keeps the thing under
    # test the dedupe rather than the quote binding: if each thread re-read the
    # plan, a compile that flipped the status mid-race could make one of them
    # fail the binding instead of the dedupe, and this test would start passing
    # for the wrong reason.
    sig = signature_for(BEAT)
    at_the_line = threading.Barrier(n, timeout=10)
    codes: list[int | None] = [None] * n
    # Per-slot dicts, not `[{}] * n` -- that is n references to ONE dict, and a
    # future reader who mutates a slot instead of replacing it would silently
    # write to every slot at once.
    bodies: list[dict] = [{} for _ in range(n)]

    def fire(i):
        at_the_line.wait()
        r = compile_beat(client, BEAT, signature=sig)
        codes[i] = r.status_code
        bodies[i] = r.json()

    threads = [threading.Thread(target=fire, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)
    assert all(c is not None for c in codes), "a compile request never returned"
    return codes, bodies


def test_two_concurrent_compiles_dispatch_exactly_one(studio):
    """TG-S4-04, at the level the audit reproduced it.

    The 200/409 split says the second caller was told the truth. `calls == 1`
    says it was told the truth *because* nothing was handed to a worker -- which
    is the only version of this guard that saves any money.
    """
    client, compiles = studio

    codes, bodies = _race(client)

    # The dispatch count goes FIRST, and the order is the test. Asserting the
    # status split first was this test committing the error its own docstring
    # warns about: remove the dedupe and the codes are [200, 200], so the status
    # assertion fails and the money assertion never runs -- the test would have
    # reported a status mismatch and never once demonstrated the second
    # dispatch. A cheaper assertion that fails first hides the expensive one.
    assert compiles.entered.wait(timeout=10), "the accepted compile never ran"
    compiles.release_and_settle()
    assert compiles.calls == 1, (
        f"{compiles.calls} compiles were dispatched for one beat; every dispatch "
        f"past the first re-buys that beat's ai_video shots")

    # Only then how it was reported.
    assert sorted(codes) == [200, 409], f"expected one 200 and one 409, got {codes}"

    winner = bodies[codes.index(200)]
    loser = bodies[codes.index(409)]
    assert winner["started"] is True and winner["job"] == f"director:{BEAT}"
    assert loser["ok"] is False
    assert "already running" in loser["error"]


def test_the_refused_caller_is_never_told_its_compile_started(studio):
    """The older shape of this defect: 409's content, not just its code.

    `start_job` returned False and the endpoint reported `{"started": true}`
    anyway, so beats were silently dropped while the caller believed they had
    begun. A refusal that reads as an acceptance is worse than either.

    Asserted over ALL the bodies before anything is indexed by status. Picking
    the loser out with `codes.index(409)` is the same ordering trap as above and
    a worse version of it: under the regression this test exists for there is no
    409 to find, so `index` raises ValueError and neither content assertion runs.
    The test would error out instead of reporting that two callers were both
    told their compile had started.
    """
    client, compiles = studio

    codes, bodies = _race(client)

    claimed = [b for b in bodies if b.get("started") is True]
    assert len(claimed) == 1, (
        f"{len(claimed)} of {len(bodies)} callers were told their compile "
        f"started; exactly one beat was dispatched, so any other claim is a "
        f"beat silently dropped while its caller was told it had begun")

    # Then, separately, that the refusal reads as one.
    refused = [b for b in bodies if b.get("started") is not True]
    assert len(refused) == 1
    assert refused[0].get("ok") is False
    assert 409 in codes, f"the refused caller was not answered 409: {codes}"


def test_the_guard_is_a_dedupe_and_not_a_permanent_block(studio):
    """The other direction, or the test above passes against `return False`.

    Once the first compile has finished the beat must be compilable again --
    a re-compile after an edit is ordinary work, and a guard that refused it
    would score perfectly on the race above while breaking the product.

    Dispatch before status here too, for the same reason as above: a guard stuck
    refusing answers 409, and asserting that first would report a status
    mismatch rather than the fact that matters -- the second compile never ran.
    """
    client, compiles = studio

    first = compile_beat(client, BEAT)
    assert compiles.entered.wait(timeout=10)
    compiles.release_and_settle()
    assert compiles.calls == 1
    assert first.status_code == 200

    compiles.entered.clear()
    compiles.proceed.clear()

    second = compile_beat(client, BEAT)
    compiles.entered.wait(timeout=10)
    compiles.release_and_settle()
    assert compiles.calls == 2, "a finished compile still blocked its beat"
    assert second.status_code == 200
