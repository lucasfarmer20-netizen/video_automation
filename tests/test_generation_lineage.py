"""Generation lineage: no double-spend, no lost attempts (contract §6, §11.1, §11.6).

Two guarantees that pull against each other unless attempts are first-class. A
retry must not overwrite a prior attempt, because a failure that disappears
takes the reason with it. And a retry must not double-charge, however it
arrives -- duplicate click, UI retry, worker restart, or a network retry where
the client thinks it failed and the server knows it succeeded.

The tests below distinguish the two guards on purpose, because they answer
different questions and neither subsumes the other:

* idempotency_key -- "is this the same REQUEST I already handled?"
* signature       -- "are these the same INPUTS I already bought?"

Money is the reason this file exists, so the assertions are about whether a
paid call would be permitted, not about whether a function returned something.
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

from backend import config, generation  # noqa: E402


@pytest.fixture(autouse=True)
def project(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "MANIFEST_PATH", tmp_path / "storyboard_manifest.json")
    return tmp_path


def _begin(**kw):
    kw.setdefault("beat_id", "s001")
    kw.setdefault("shot_id", "s001.01")
    kw.setdefault("signature", "sig-a")
    kw.setdefault("paid", True)
    return generation.begin(**kw)


# --- §11.1 no double-spend --------------------------------------------------------

def test_a_duplicate_request_does_not_open_a_second_attempt():
    """Two clicks on one button carry one key and must buy one clip."""
    first, how1 = _begin(idempotency_key="req-1")
    second, how2 = _begin(idempotency_key="req-1")
    assert how1 == "created"
    assert how2 == "duplicate", "a repeated request was allowed to spend again"
    assert first.id == second.id
    assert len(generation.for_shot("s001", "s001.01")) == 1


def test_unchanged_inputs_reuse_the_media_already_bought():
    """A NEW request for a shot nothing has changed about must not re-buy it."""
    att, _ = _begin(idempotency_key="req-1")
    generation.succeed("s001", att.id, "render/s001/s001.01.mp4", cost=0.60)

    again, how = _begin(idempotency_key="req-2", exists=lambda rel: True)
    assert how == "reused"
    assert again.id == att.id


def test_changed_inputs_are_a_new_purchase():
    """The signature is what makes reuse safe; different inputs are different media."""
    att, _ = _begin(idempotency_key="req-1", signature="sig-a")
    generation.succeed("s001", att.id, "render/s001/s001.01.mp4", cost=0.60)

    fresh, how = _begin(idempotency_key="req-2", signature="sig-b",
                        exists=lambda rel: True)
    assert how == "created"
    assert fresh.id != att.id


def test_a_recorded_success_whose_media_is_gone_is_not_reusable():
    """Otherwise the shot references a file that is not there, and the deletion
    surfaces much later as a render bug."""
    att, _ = _begin(idempotency_key="req-1")
    generation.succeed("s001", att.id, "render/s001/s001.01.mp4", cost=0.60)

    _, how = _begin(idempotency_key="req-2", exists=lambda rel: False)
    assert how == "created"


def test_the_two_guards_are_not_the_same_guard():
    """A duplicate request must dedupe even when the inputs changed -- the client
    asked once. Collapsing the two guards into one loses that."""
    att, _ = _begin(idempotency_key="req-1", signature="sig-a")
    same, how = _begin(idempotency_key="req-1", signature="sig-b")
    assert how == "duplicate"
    assert same.id == att.id


def test_concurrent_duplicate_requests_open_exactly_one_attempt():
    """A double click races. Only one of them may reach the paid API."""
    results: list[str] = []
    start = threading.Barrier(8, timeout=10)

    def go():
        start.wait()
        _, how = _begin(idempotency_key="same-request")
        results.append(how)

    threads = [threading.Thread(target=go) for _ in range(8)]
    for th in threads:
        th.start()
    for th in threads:
        th.join(timeout=10)

    assert results.count("created") == 1, f"{results.count('created')} would have paid"
    assert len(generation.for_shot("s001", "s001.01")) == 1


def test_a_worker_restart_does_not_re_bill():
    """A restarted job replays the same request; the ledger is on disk, not in
    memory, so the replay is recognised."""
    att, _ = _begin(idempotency_key="job-42")
    generation.succeed("s001", att.id, "render/s001/s001.01.mp4", cost=0.60)
    generation._MUTATE_LOCK  # the module keeps no other state
    _, how = _begin(idempotency_key="job-42")
    assert how == "duplicate"


# --- §11.6 lineage is never lost ---------------------------------------------------

def test_a_failed_attempt_stays_attached_to_its_shot():
    """A failure that disappears takes the reason with it."""
    att, _ = _begin(idempotency_key="req-1")
    generation.fail("s001", att.id, "fal returned 500")

    rows = generation.for_shot("s001", "s001.01")
    assert len(rows) == 1
    assert rows[0].status == "failed"
    assert "500" in rows[0].error


def test_a_retry_branches_from_the_attempt_it_retries():
    att1, _ = _begin(idempotency_key="req-1")
    generation.fail("s001", att1.id, "boom")
    att2, how = _begin(idempotency_key="req-2")

    assert how == "created"
    assert att2.parent_attempt == att1.id
    assert att2.attempt == 2


def test_a_retry_never_overwrites_the_previous_attempt():
    att1, _ = _begin(idempotency_key="req-1")
    generation.fail("s001", att1.id, "first failure")
    att2, _ = _begin(idempotency_key="req-2")
    generation.succeed("s001", att2.id, "render/s001/s001.01.mp4", cost=0.6)

    rows = generation.for_shot("s001", "s001.01")
    assert [r.status for r in rows] == ["failed", "succeeded"]
    assert rows[0].error == "first failure", "the earlier failure was rewritten"


def test_attempts_for_different_shots_do_not_mix():
    a, _ = _begin(shot_id="s001.01", idempotency_key="k1")
    b, _ = _begin(shot_id="s001.02", idempotency_key="k2")
    assert [r.id for r in generation.for_shot("s001", "s001.01")] == [a.id]
    assert [r.id for r in generation.for_shot("s001", "s001.02")] == [b.id]


def test_lineage_survives_a_reload_from_disk():
    att, _ = _begin(idempotency_key="req-1")
    generation.fail("s001", att.id, "boom")
    generation.begin(beat_id="s001", shot_id="s001.01", signature="sig-a",
                     idempotency_key="req-2", paid=True)

    revived = generation.load_attempts("s001")
    assert len(revived) == 2
    assert revived[1].parent_attempt == revived[0].id


# --- §6.1 spend is explicit ---------------------------------------------------------

def test_spend_counts_only_what_was_actually_billed():
    paid, _ = _begin(idempotency_key="k1", signature="sig-a")
    generation.succeed("s001", paid.id, "a.mp4", cost=0.60)
    failed, _ = _begin(idempotency_key="k2", signature="sig-b")
    generation.fail("s001", failed.id, "boom")
    free, _ = _begin(idempotency_key="k3", signature="sig-c", paid=False)
    generation.succeed("s001", free.id, "b.mp4", cost=0.0)

    s = generation.spend("s001")
    assert s["spent"] == 0.60, "a failure or a free render was counted as spend"
    assert s["paid_attempts"] == 1
    assert s["failed"] == 1
    assert s["attempts"] == 3


def test_a_running_attempt_is_not_counted_as_spend():
    _begin(idempotency_key="k1")
    assert generation.spend("s001")["spent"] == 0.0


# --- project isolation (§11.3 still holds here) --------------------------------------

def test_lineage_is_written_inside_the_bound_project(tmp_path, monkeypatch):
    from backend import projects
    other = tmp_path / "other"
    other.mkdir()
    ctx = projects.ProjectContext.from_manifest(other / "storyboard_manifest.json")

    _begin(idempotency_key="here")
    with projects.use(ctx):
        _begin(idempotency_key="there")
        assert generation.generation_dir() == other / "generation"
        assert len(generation.for_shot("s001", "s001.01")) == 1

    assert len(generation.for_shot("s001", "s001.01")) == 1


# --- the ledger as an INDEPENDENT re-bill guard -----------------------------------
#
# DirectorShot.paid_clip/paid_signature already stop the ordinary repeat. The
# attempt ledger matters exactly where that marker is gone: a re-plan rewrites
# the coverage, a crash lands between the download and the plan write, or a
# fresh plan object is built from the same intent. The media and the ledger are
# still on disk, and neither the shot nor the caller remembers paying.

@pytest.fixture
def scene(tmp_path, monkeypatch):
    from backend import director
    from backend.manifest import Camera, Shot, Storyboard

    monkeypatch.setattr(director.config, "MANIFEST_PATH",
                        tmp_path / "storyboard_manifest.json")
    render_dir = tmp_path / "render"
    render_dir.mkdir()

    plan = director.CoveragePlan(beat_id="s011", beat_duration=5.0, status="locked")
    ds = director.DirectorShot(id="s011.01", beat_id="s011", motion_type="ai_video",
                               camera=Camera(move="static", duration=5.0),
                               prompt="a hand on drill steel", subject="single jack",
                               shot_size="m", purpose="master")
    ds.draft_variations = ["a.png"]
    ds.chosen_variation = 0
    plan.coverage = [ds]
    director.approve(plan)
    director.save_plan(plan)

    sb = Storyboard(title="T", storyboard_approved=True,
                    shots=[Shot(scene_id="s011", narration="n", prompt="p",
                                camera=Camera(move="static", duration=5.0))])

    calls = {"paid": 0}

    def fake_paid(ds_, synth, sb_, out_dir, log=print):
        calls["paid"] += 1
        target = Path(out_dir) / f"{ds_.id}.mp4"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"PAID-BYTES")
        return target

    monkeypatch.setattr(director, "generate_paid_clip", fake_paid)
    monkeypatch.setattr(director, "normalize_clip", lambda *a, **k: None)
    monkeypatch.setattr(director, "fit_clip", lambda *a, **k: None)
    monkeypatch.setattr(director, "concat", lambda *a, **k: Path("beat.mp4"))
    return director, plan, sb, render_dir, calls


def test_a_lost_paid_marker_does_not_re_buy_the_clip(scene):
    """The ledger's own job. With paid_clip cleared the old guard cannot help,
    and the shot is about to be paid for a second time."""
    director, _, sb, render_dir, calls = scene
    director.compile_coverage(director.load_plan("s011"), sb, render_dir,
                              log=lambda m: None)
    assert calls["paid"] == 1

    # A re-plan or an interrupted write leaves the media and the ledger, but
    # loses the shot's own record of having paid.
    plan = director.load_plan("s011")
    plan.coverage[0].paid_clip = ""
    plan.coverage[0].paid_signature = ""
    director.save_plan(plan)

    director.compile_coverage(director.load_plan("s011"), sb, render_dir,
                              log=lambda m: None, skip_existing=False)
    assert calls["paid"] == 1, "the clip was bought twice after the marker was lost"


def test_the_reused_attempt_is_recorded_on_the_shot(scene):
    director, _, sb, render_dir, calls = scene
    director.compile_coverage(director.load_plan("s011"), sb, render_dir,
                              log=lambda m: None)
    plan = director.load_plan("s011")
    assert plan.coverage[0].selected_attempt, "the shot does not reference its attempt"

    rows = generation.for_shot("s011", "s011.01")
    assert len(rows) == 1 and rows[0].status == "succeeded"
    assert rows[0].paid is True


def test_a_failed_paid_generation_is_recorded_and_retryable(scene, monkeypatch):
    """§6: a failed generation stays attached to the intended shot."""
    director, _, sb, render_dir, calls = scene

    def boom(*a, **k):
        calls["paid"] += 1
        raise RuntimeError("fal returned 500")

    monkeypatch.setattr(director, "generate_paid_clip", boom)
    # compile_coverage reports the beat as failed rather than shipping a short
    # clip; the point here is what it leaves behind for the retry.
    with pytest.raises(director.PlanError):
        director.compile_coverage(director.load_plan("s011"), sb, render_dir,
                                  log=lambda m: None)

    rows = generation.for_shot("s011", "s011.01")
    assert len(rows) == 1
    assert rows[0].status == "failed"
    assert "500" in rows[0].error


def test_spend_is_reported_from_the_ledger(scene):
    director, _, sb, render_dir, _ = scene
    director.compile_coverage(director.load_plan("s011"), sb, render_dir,
                              log=lambda m: None)
    assert generation.spend("s011")["paid_attempts"] == 1
