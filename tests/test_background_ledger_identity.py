"""TG-S4-05: a Director background job writes its ledger into the project it
was ENQUEUED against -- money in the right account.

`tests/test_project_isolation.py` proves the generic mechanism: a job captures
its context at enqueue and rebinds it in the worker thread. What it proves it
with is a probe body that reads `config.project_dir()`. That is the mechanism,
not the consequence, and the consequence is the part that costs money.

This is the same defect class as issues #7 and #8 -- work landing in the wrong
project -- with the worst payload it has. A generation ledger in the wrong
project is not a misplaced file: it is the record of what a paid clip cost,
filed against a film that never bought it. Project A then reports $0.00 for a
charge that happened, and `begin()` there sees no running attempt and lets the
next request buy the clip again. Both halves of §6.1 fail at once, silently,
while the job reports success.

So the assertions here are about the ledger and about spend, driven through the
production path: a real HTTP compile, a real `start_job`, a real worker thread,
the real `compile_coverage` calling the real `generation.begin`/`succeed`. The
project is switched underneath the job while it is queued, exactly as selecting
another film in the studio does.
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
from signed_compile import compile_beat  # noqa: E402

from backend import config, director, generation, projects  # noqa: E402
from backend.manifest import Camera, Shot, Storyboard  # noqa: E402

BEAT = "s011"
SHOT = "s011.01"


def _project(tmp_path: Path, name: str) -> projects.ProjectContext:
    d = tmp_path / name
    d.mkdir(parents=True, exist_ok=True)
    mf = d / "storyboard_manifest.json"
    mf.write_text('{"title": "%s", "shots": []}' % name, encoding="utf-8")
    return projects.ProjectContext.from_manifest(mf)


def _board() -> Storyboard:
    return Storyboard(title="T", script_locked=True, storyboard_approved=True,
                      shots=[Shot(scene_id=BEAT, narration="n", prompt="p",
                                  camera=Camera(move="static", duration=5.0))])


def _locked_plan() -> director.CoveragePlan:
    plan = director.CoveragePlan(beat_id=BEAT, beat_duration=5.0, status="locked")
    ds = director.DirectorShot(id=SHOT, beat_id=BEAT, motion_type="ai_video",
                               camera=Camera(move="static", duration=5.0),
                               prompt="a hand on drill steel", subject="single jack",
                               shot_size="m", purpose="master")
    # Pre-supplied so the compile goes straight to the paid branch without
    # calling the still-image backend.
    ds.draft_variations = ["a.png"]
    ds.chosen_variation = 0
    plan.coverage = [ds]
    director.approve(plan)
    return plan


@pytest.fixture
def two_projects(tmp_path, monkeypatch):
    """Projects A and B, a client on A, and a compile held at the starting line.

    The hold is a barrier around the *real* `compile_coverage`, not a
    replacement for it: the job must not begin until the studio has switched to
    B, or the switch would land after the ledger was already written and prove
    nothing.
    """
    from fastapi.testclient import TestClient
    from backend import main as M

    a, b = _project(tmp_path, "alpha"), _project(tmp_path, "bravo")
    boards = {a.project_id: _board(), b.project_id: _board()}

    monkeypatch.setattr(config, "MANIFEST_PATH", a.manifest_path)
    monkeypatch.setattr(M, "get_active_manifest_path", lambda: str(a.manifest_path))
    monkeypatch.setattr(
        M, "get_current_project",
        lambda: boards[projects.bound().project_id])

    with projects.use(a):
        director.save_plan(_locked_plan())

    paid = {"calls": 0}

    def fake_paid(ds, synth, sb, out_dir, log=print):
        paid["calls"] += 1
        target = Path(out_dir) / f"{ds.id}.mp4"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"PAID-BYTES")
        return target

    monkeypatch.setattr(director, "generate_paid_clip", fake_paid)
    monkeypatch.setattr(director, "normalize_clip", lambda *a_, **k: None)
    monkeypatch.setattr(director, "fit_clip", lambda *a_, **k: None)
    monkeypatch.setattr(director, "concat", lambda *a_, **k: Path("beat.mp4"))
    monkeypatch.setattr(director, "probe_seconds", lambda *a_, **k: 5.0)

    real_compile = director.compile_coverage
    at_the_gate = threading.Event()
    proceed = threading.Event()

    def held_compile(*a_, **k):
        at_the_gate.set()
        proceed.wait(timeout=10)
        return real_compile(*a_, **k)

    monkeypatch.setattr(M.director, "compile_coverage", held_compile)

    original_fallback = projects.current()
    client = TestClient(M.app, raise_server_exceptions=False)
    try:
        # `real_compile` is yielded because `M.director` IS `backend.director`
        # -- one module object -- so after the patch above, the name
        # `director.compile_coverage` resolves to `held_compile` inside the
        # tests too. A test meaning to drive a compile directly would take the
        # barrier instead, and pass only for as long as `proceed` happened to be
        # set: re-arm the hold, or reorder the helper, and it blocks for the
        # full timeout and then runs anyway. A slow pass gets diagnosed as flake
        # rather than as a test calling something it did not mean to.
        yield client, a, b, at_the_gate, proceed, paid, real_compile
    finally:
        proceed.set()
        me = threading.current_thread()
        for t in threading.enumerate():
            if t is not me and t.name.startswith("job-"):
                t.join(timeout=10)
        projects.set_fallback(original_fallback)


def _compile_on_a_then_switch_to_b(client, a, b, at_the_gate, proceed):
    """Enqueue against A, move the whole process to B, then let the job run."""
    r = compile_beat(client, BEAT, headers={"X-Project-Id": a.project_id})
    assert r.status_code == 200, r.text
    assert at_the_gate.wait(timeout=10), "the compile job never started"

    # The studio switches film while the job is still queued. Both halves, so
    # nothing unbound can still resolve to A by accident.
    config.MANIFEST_PATH = b.manifest_path
    projects.set_fallback(b)

    proceed.set()
    me = threading.current_thread()
    for t in threading.enumerate():
        if t is not me and t.name.startswith("job-"):
            t.join(timeout=15)


def test_a_background_generation_ledger_lands_in_the_captured_project(two_projects):
    """TG-S4-05. The ledger belongs to the film that was compiling, full stop.

    Both locations are read before either is asserted, and the wrong-account
    half is asserted first -- it is the sharper sentence, and ordering the
    missing-record half ahead of it would report "A has no ledger" while never
    saying where the money actually went.
    """
    client, a, b, at_the_gate, proceed, paid, _real = two_projects

    _compile_on_a_then_switch_to_b(client, a, b, at_the_gate, proceed)
    assert paid["calls"] == 1, "the paid clip was never generated; nothing to file"

    in_a = (a.root / "generation" / f"{BEAT}.json").is_file()
    in_b = (b.root / "generation" / f"{BEAT}.json").is_file()

    assert not in_b, "another film was billed for a clip it did not buy"
    assert in_a, "the project that paid for the clip has no record of it"


def test_the_project_that_paid_is_the_project_that_reports_the_spend(two_projects):
    """The consequence, in the units that matter.

    A ledger in the wrong project is a charge reported against the wrong film.
    Reading spend from both sides is what makes that visible: A must carry the
    $0.60, and B must carry nothing at all.

    Every reading is taken BEFORE anything is asserted, and the money is
    asserted before the lineage. Interleaving them hid the half that matters:
    `len(rows) == 1` is checked against A, fails first under the regression --
    A has no rows, because they went to B -- and `spend_b["spent"] == 0.0`, the
    assertion that actually says another film was billed, never ran at all.
    """
    client, a, b, at_the_gate, proceed, paid, _real = two_projects

    _compile_on_a_then_switch_to_b(client, a, b, at_the_gate, proceed)

    with projects.use(a):
        rows_a = generation.for_shot(BEAT, SHOT)
        spend_a = generation.spend(BEAT)
        # The price the shot was QUOTED at, from the plan in A. Same function the
        # charge goes through, so "billed what it was quoted" is what this
        # compares -- not one hardcoded figure against another.
        billed_for = director.paid_clip_price(director.load_plan(BEAT).coverage[0])
    with projects.use(b):
        rows_b = generation.load_attempts(BEAT)
        spend_b = generation.spend(BEAT)

    # Money first, and the wrong-account half first within it: that is the
    # sentence this whole test exists to be able to say.
    assert spend_b["spent"] == 0.0, \
        f"a film was billed ${spend_b['spent']:.2f} for another film's clip"
    assert spend_a["spent"] == billed_for, \
        f"the paying project reports {spend_a['spent']}, not what it was billed"
    assert spend_b["paid_attempts"] == 0
    assert spend_a["paid_attempts"] == 1

    # Then the lineage the figures were computed from.
    assert rows_b == [], "the other film's lineage was written into this one"
    assert len(rows_a) == 1, f"expected one attempt in the paying project, got {len(rows_a)}"
    assert rows_a[0].status == generation.SUCCEEDED
    assert rows_a[0].paid is True


def test_the_captured_project_still_blocks_a_second_charge_after_the_switch(
        two_projects):
    """The other half of a misfiled ledger, and the more expensive one.

    A ledger written into B leaves A with no record of the attempt, so `begin()`
    in A sees an unbought shot and permits a second purchase. Asserting the file
    landed correctly is not enough on its own -- what has to hold is that the
    re-bill guard in the paying project can still see it.
    """
    client, a, b, at_the_gate, proceed, paid, real_compile = two_projects

    _compile_on_a_then_switch_to_b(client, a, b, at_the_gate, proceed)
    assert paid["calls"] == 1

    with projects.use(a):
        plan = director.load_plan(BEAT)
        # A re-plan or an interrupted write loses the shot's own paid marker;
        # the ledger is then the only thing standing between this shot and a
        # second charge, and it has to be in this project to do that.
        plan.coverage[0].paid_clip = ""
        plan.coverage[0].paid_signature = ""
        director.save_plan(plan)

        # `real_compile`, NOT `director.compile_coverage` -- that name is the
        # fixture's barrier-wrapped stand-in, and taking it here would make this
        # test depend on `proceed` still being set by the helper above.
        real_compile(director.load_plan(BEAT), _board(),
                     a.root / "render", log=lambda m: None,
                     skip_existing=False)

    assert paid["calls"] == 1, \
        "the clip was bought again because its ledger was filed elsewhere"
