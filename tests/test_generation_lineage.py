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


def test_a_failed_paid_generation_stays_attached_and_in_doubt(scene, monkeypatch):
    """§6: a failed generation stays attached to the intended shot.

    It stays RUNNING rather than failed: once generate_paid_clip has been
    called, whether the provider billed is unknown, and recording "failed"
    invites a retry that buys the clip again. The reason is recorded on the
    attempt so a human can resolve it."""
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
    assert rows[0].status == "running", "an unknown provider outcome recorded as failed"
    assert "500" in rows[0].error


def test_spend_is_reported_from_the_ledger(scene):
    director, _, sb, render_dir, _ = scene
    director.compile_coverage(director.load_plan("s011"), sb, render_dir,
                              log=lambda m: None)
    assert generation.spend("s011")["paid_attempts"] == 1


# --- S4-01: the charge-before-success crash window --------------------------------

def test_a_crash_after_charging_does_not_buy_the_clip_again(scene):
    """The window Slice 4 claimed to cover and did not.

    The provider is reached and the process dies before succeed() or the plan
    marker is written. The attempt is left RUNNING, and nothing can tell whether
    the money was spent. Creating another attempt is a second bill, so the
    replay must refuse and ask a human to resolve it.
    """
    director, _, sb, render_dir, calls = scene

    def charge_then_die(ds_, synth, sb_, out_dir, log=print):
        calls["paid"] += 1
        target = Path(out_dir) / f"{ds_.id}.mp4"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"PAID-BYTES")
        raise KeyboardInterrupt("container reclaimed mid-generation")

    import pytest as _pytest
    monkey = _pytest.MonkeyPatch()
    monkey.setattr(director, "generate_paid_clip", charge_then_die)
    with _pytest.raises(BaseException):
        director.compile_coverage(director.load_plan("s011"), sb, render_dir,
                                  log=lambda m: None)
    monkey.undo()
    assert calls["paid"] == 1

    # The replay. Nothing about the shot has changed, and the attempt is stuck.
    with _pytest.raises(director.PlanError):
        director.compile_coverage(director.load_plan("s011"), sb, render_dir,
                                  log=lambda m: None, skip_existing=False)
    # compile_coverage reports an aggregate; the reason lives on the shot.
    assert "still recorded as running" in director.load_plan("s011").coverage[0].error
    assert calls["paid"] == 1, "the crash window bought the clip a second time"


def test_abandoning_a_stuck_attempt_unblocks_the_retry(scene):
    """The explicit recovery. A human says the outcome is unknown and accepts
    the risk; the machine never makes that call for them."""
    director, _, sb, render_dir, calls = scene

    def charge_then_die(ds_, synth, sb_, out_dir, log=print):
        calls["paid"] += 1
        raise KeyboardInterrupt("container reclaimed")

    import pytest as _pytest
    monkey = _pytest.MonkeyPatch()
    monkey.setattr(director, "generate_paid_clip", charge_then_die)
    with _pytest.raises(BaseException):
        director.compile_coverage(director.load_plan("s011"), sb, render_dir,
                                  log=lambda m: None)
    monkey.undo()

    stuck = [a for a in generation.for_shot("s011", "s011.01")
             if a.status == "running"]
    assert len(stuck) == 1
    generation.abandon("s011", stuck[0].id, "checked the provider dashboard")

    director.compile_coverage(director.load_plan("s011"), sb, render_dir,
                              log=lambda m: None, skip_existing=False)
    assert calls["paid"] == 2
    rows = generation.for_shot("s011", "s011.01")
    assert [r.status for r in rows] == ["failed", "succeeded"]
    assert "abandoned" in rows[0].error


# --- S4-02: an unreadable ledger must not read as an empty one --------------------

def test_a_corrupt_ledger_refuses_rather_than_starting_over():
    att, _ = _begin(idempotency_key="k1")
    generation.succeed("s001", att.id, "a.mp4", cost=0.60)
    path = generation.ledger_path("s001")
    original = path.read_bytes()

    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(generation.LedgerUnreadable):
        _begin(idempotency_key="k2")

    assert path.read_bytes() == b"{not json", "the corrupt ledger was overwritten"
    assert original != b"{not json"


def test_a_read_fault_refuses_rather_than_erasing_history():
    """One transient GCS read fault used to permit a second charge AND destroy
    the record that would have prevented the next one.

    Its own MonkeyPatch instance on purpose: the fixture-provided one also holds
    the autouse project patch, so undoing it here would move the project out
    from under the assertions.
    """
    att, _ = _begin(idempotency_key="k1")
    generation.succeed("s001", att.id, "a.mp4", cost=0.60)
    path = generation.ledger_path("s001")
    before = path.read_bytes()

    real = Path.read_text

    def flaky(self, *a, **k):
        if self == path:
            raise OSError(5, "transient input/output error")
        return real(self, *a, **k)

    mp = pytest.MonkeyPatch()
    mp.setattr(Path, "read_text", flaky)
    try:
        with pytest.raises(generation.LedgerUnreadable):
            _begin(idempotency_key="k2")
    finally:
        mp.undo()

    assert path.read_bytes() == before, "the paid success was erased"
    assert generation.spend("s001")["spent"] == 0.60


def test_a_genuinely_missing_ledger_is_still_empty_history():
    """Failing closed must not mean refusing the first ever generation."""
    att, how = _begin(idempotency_key="k1")
    assert how == "created"
    assert att.attempt == 1


# --- S4-03: a terminal attempt is billing truth ------------------------------------

def test_a_succeeded_attempt_cannot_be_rewritten_as_failed():
    """A late or duplicated error callback used to zero the reported spend."""
    att, _ = _begin(idempotency_key="k1")
    generation.succeed("s001", att.id, "a.mp4", cost=0.60)
    path = generation.ledger_path("s001")
    before = path.read_bytes()

    with pytest.raises(generation.TerminalConflict):
        generation.fail("s001", att.id, "a late error callback")

    assert path.read_bytes() == before, "the stored record changed"
    assert generation.spend("s001")["spent"] == 0.60
    assert generation.for_shot("s001", "s001.01")[0].status == "succeeded"


def test_a_failed_attempt_cannot_be_rewritten_as_succeeded():
    att, _ = _begin(idempotency_key="k1")
    generation.fail("s001", att.id, "boom")
    with pytest.raises(generation.TerminalConflict):
        generation.succeed("s001", att.id, "a.mp4", cost=0.60)
    assert generation.spend("s001")["spent"] == 0.0


def test_repeating_the_same_completion_is_a_no_op():
    """A retried callback is not a new fact."""
    att, _ = _begin(idempotency_key="k1")
    generation.succeed("s001", att.id, "a.mp4", cost=0.60)
    again = generation.succeed("s001", att.id, "a.mp4", cost=0.60)
    assert again.status == "succeeded"
    assert generation.spend("s001")["paid_attempts"] == 1


# --- S4-R01: recovery must be reachable through the product -----------------------

@pytest.fixture
def api(scene):
    """The compile fixture plus an HTTP client bound to the same project."""
    from fastapi.testclient import TestClient
    from backend import main as M
    director, plan, sb, render_dir, calls = scene
    monkey = pytest.MonkeyPatch()
    monkey.setattr(M, "get_current_project", lambda: sb)
    monkey.setattr(M.config, "MANIFEST_PATH", director.config.MANIFEST_PATH)
    monkey.setattr(M, "get_active_manifest_path",
                   lambda: str(director.config.MANIFEST_PATH))
    client = TestClient(M.app, raise_server_exceptions=False)
    yield client, director, sb, render_dir, calls
    monkey.undo()


def _strand(director, sb, render_dir, calls):
    """Leave a paid attempt stuck in doubt, the way a provider timeout does."""
    mp = pytest.MonkeyPatch()

    def timeout(*a, **k):
        calls["paid"] += 1
        raise RuntimeError("provider timeout after submit")

    mp.setattr(director, "generate_paid_clip", timeout)
    with pytest.raises(director.PlanError):
        director.compile_coverage(director.load_plan("s011"), sb, render_dir,
                                  log=lambda m: None)
    mp.undo()


def test_a_stranded_beat_is_visible_through_the_api(api):
    client, director, sb, render_dir, calls = api
    _strand(director, sb, render_dir, calls)

    body = client.get("/api/generation/s011").json()
    assert body["ok"] is True
    assert len(body["unresolved"]) == 1
    assert "provider timeout" in body["unresolved"][0]["error"]


def test_a_stranded_beat_can_be_recovered_without_a_python_shell(api):
    """S4-R01. The only recovery used to be calling generation.abandon() from
    internal Python, which makes an ordinary provider timeout a permanently
    uncompilable beat for anyone without repository access."""
    client, director, sb, render_dir, calls = api
    _strand(director, sb, render_dir, calls)
    assert calls["paid"] == 1

    stuck = client.get("/api/generation/s011").json()["unresolved"][0]
    r = client.post("/api/generation/s011/" + stuck["id"] + "/abandon",
                    json={"reason": "checked the provider dashboard, no charge"})
    assert r.status_code == 200

    director.compile_coverage(director.load_plan("s011"), sb, render_dir,
                              log=lambda m: None, skip_existing=False)
    assert calls["paid"] == 2
    rows = generation.for_shot("s011", "s011.01")
    assert [x.status for x in rows] == ["failed", "succeeded"]
    assert "no charge" in rows[0].error


def test_abandoning_requires_a_stated_reason(api):
    """It may be writing off a real charge, so the record has to say why."""
    client, director, sb, render_dir, calls = api
    _strand(director, sb, render_dir, calls)
    stuck = client.get("/api/generation/s011").json()["unresolved"][0]
    assert client.post("/api/generation/s011/" + stuck["id"] + "/abandon",
                       json={"reason": "   "}).status_code == 400
    assert generation.for_shot("s011", "s011.01")[0].status == "running"


def test_the_plan_payload_reports_a_stuck_attempt(api):
    """The block belongs where the plan is shown, not only in a separate view."""
    client, director, sb, render_dir, calls = api
    _strand(director, sb, render_dir, calls)
    plan = client.get("/api/director/plan/s011").json()["plan"]
    assert len(plan["unresolved_attempts"]) == 1


def test_an_unreadable_ledger_is_reported_not_shown_as_empty(api):
    client, director, sb, render_dir, calls = api
    _strand(director, sb, render_dir, calls)
    generation.ledger_path("s011").write_text("{not json", encoding="utf-8")
    r = client.get("/api/generation/s011")
    assert r.status_code == 409
    assert r.json()["unreadable"] is True


# --- S4-R02: the provider-dispatch boundary ---------------------------------------

def test_a_failure_before_dispatch_is_an_ordinary_retryable_failure(scene):
    """S4-R02. A permission error deleting a stale file recorded a
    possibly-billed attempt with no reason, and stranded the beat behind a
    recovery step for a risk that was never taken."""
    director, _, sb, render_dir, calls = scene
    target = render_dir / "s011" / "s011.01.mp4"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"a stale free render")

    real_unlink = Path.unlink

    def deny(self, *a, **k):
        if self == target:
            raise PermissionError(13, "storage denied the delete")
        return real_unlink(self, *a, **k)

    mp = pytest.MonkeyPatch()
    mp.setattr(Path, "unlink", deny)
    # skip_existing=False: the stale file at the target would otherwise make the
    # shot look already rendered and skip the paid branch entirely.
    with pytest.raises(director.PlanError):
        director.compile_coverage(director.load_plan("s011"), sb, render_dir,
                                  log=lambda m: None, skip_existing=False)
    mp.undo()

    assert calls["paid"] == 0, "the provider was called"
    rows = generation.for_shot("s011", "s011.01")
    assert rows[0].status == "failed", "a pre-dispatch failure was left in doubt"
    assert "before dispatch" in rows[0].error
    # And therefore no money is exposed: the risk this attempt reports is the
    # risk it actually took, which is none.
    assert generation.spend("s011")["at_risk"] == 0.0


def test_a_pre_dispatch_failure_does_not_strand_the_beat(scene):
    """Because there is no billing uncertainty, the retry must just work."""
    director, _, sb, render_dir, calls = scene
    target = render_dir / "s011" / "s011.01.mp4"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"stale")

    real_unlink = Path.unlink
    state = {"deny": True}

    def deny(self, *a, **k):
        if self == target and state["deny"]:
            raise PermissionError(13, "storage denied the delete")
        return real_unlink(self, *a, **k)

    mp = pytest.MonkeyPatch()
    mp.setattr(Path, "unlink", deny)
    with pytest.raises(director.PlanError):
        director.compile_coverage(director.load_plan("s011"), sb, render_dir,
                                  log=lambda m: None, skip_existing=False)
    state["deny"] = False
    director.compile_coverage(director.load_plan("s011"), sb, render_dir,
                              log=lambda m: None, skip_existing=False)
    mp.undo()
    assert calls["paid"] == 1


# --- S4-R03: identical means identical FACTS --------------------------------------

def test_the_same_status_with_a_different_output_is_a_conflict():
    att, _ = _begin(idempotency_key="k1")
    generation.succeed("s001", att.id, "first.mp4", cost=0.60)
    with pytest.raises(generation.TerminalConflict):
        generation.succeed("s001", att.id, "other.mp4", cost=0.60)
    assert generation.for_shot("s001", "s001.01")[0].output == "first.mp4"


def test_the_same_status_with_a_different_cost_is_a_conflict():
    att, _ = _begin(idempotency_key="k1")
    generation.succeed("s001", att.id, "first.mp4", cost=0.60)
    with pytest.raises(generation.TerminalConflict):
        generation.succeed("s001", att.id, "first.mp4", cost=99.00)
    assert generation.spend("s001")["spent"] == 0.60


def test_a_failure_with_a_different_reason_is_a_conflict():
    att, _ = _begin(idempotency_key="k1")
    generation.fail("s001", att.id, "provider failed")
    with pytest.raises(generation.TerminalConflict):
        generation.fail("s001", att.id, "something else entirely")
    assert generation.for_shot("s001", "s001.01")[0].error == "provider failed"


def test_abandon_cannot_overwrite_a_recorded_failure():
    """fail() racing abandon() let both callers believe they wrote the reason."""
    att, _ = _begin(idempotency_key="k1")
    generation.fail("s001", att.id, "provider failed")
    with pytest.raises(generation.TerminalConflict):
        generation.abandon("s001", att.id, "human abandoned")
    assert generation.for_shot("s001", "s001.01")[0].error == "provider failed"


def test_an_exact_replay_is_still_a_no_op():
    att, _ = _begin(idempotency_key="k1")
    generation.succeed("s001", att.id, "first.mp4", cost=0.60)
    again = generation.succeed("s001", att.id, "first.mp4", cost=0.60)
    assert again.status == "succeeded"
    assert generation.spend("s001")["paid_attempts"] == 1


# --- S8-01: money that may have left the account must never report zero -----------
#
# spend() counted paid AND succeeded, so a paid attempt closed any other way
# contributed 0.00 -- including one whose clip was bought and used. The fix
# separates two facts that the old predicate ran together, and keeps them in
# separate keys because merging them would trade one inaccuracy for another:
#
#   spent   -- the record says the provider produced media. Certain.
#   at_risk -- the provider was called and nobody recorded what it did. Not.
#
# The direction of error is deliberate. Over-reporting exposure is recoverable;
# reporting $0.00 for a charge that happened is not, because nobody goes looking.

def _record(beat_id: str, **fields) -> None:
    """Put a ledger record on disk directly.

    Some of the shapes below cannot be produced through the API any more -- the
    S4-03 terminal guard stops a succeeded attempt being rewritten as failed.
    They are still on disk in every ledger written before that guard landed, and
    the money in them is no less real for being unreachable to new code. A
    ledger is a file, and this is what the file can contain.
    """
    fields.setdefault("id", beat_id + ".01.a1")
    fields.setdefault("shot_id", beat_id + ".01")
    fields.setdefault("attempt", 1)
    generation._save_attempts(
        beat_id, [generation.GenerationAttempt(beat_id=beat_id, **fields)])


def test_a_bought_clip_that_was_abandoned_still_reports_what_it_cost():
    """The reported defect, in its own words: a clip bought and used reports $0.

    The record carries a provider success -- an output path and a cost. The
    attempt was then closed as abandoned. Abandoning is a local decision to stop
    waiting; it does not un-buy the clip, and the bill does not care what status
    this row ended up with.
    """
    _record("s001", status=generation.FAILED, paid=True, cost=0.60,
            output="render/s001/s001.01.mp4",
            error=generation.ABANDONED + "wrote it off after the timeout")

    s = generation.spend("s001")
    assert s["spent"] == 0.60, "a clip that was bought and used reported $0.00"
    assert s["paid_attempts"] == 1


def test_a_bought_clip_left_in_doubt_still_reports_what_it_cost():
    """Same fact, the other unclosed shape: the media landed and the attempt was
    never closed at all."""
    _record("s001", status=generation.RUNNING, paid=True, cost=0.60,
            output="render/s001/s001.01.mp4", outcome_unknown=True,
            error="provider timeout after submit")

    s = generation.spend("s001")
    assert s["spent"] == 0.60, "a recorded provider success was ignored"
    assert s["at_risk"] == 0.0, "money the record accounts for was double-counted"


def test_recorded_media_alone_counts_as_bought():
    """A record can carry the clip without the invoice. The output path is the
    provider's success; the price then falls back to what the attempt was opened
    for, because $0.00 is the one answer that is certainly wrong."""
    _record("s001", status=generation.FAILED, paid=True,
            output="render/s001/s001.01.mp4", estimated_cost=0.60,
            error=generation.ABANDONED + "the cost never came back")
    s = generation.spend("s001")
    assert s["paid_attempts"] == 1
    assert s["spent"] == 0.60


def test_a_recorded_cost_alone_counts_as_bought():
    """The provider billed and returned nothing usable. The clip is lost; the
    money is not, and the ledger is the only place that still knows."""
    _record("s001", status=generation.FAILED, paid=True, cost=0.60,
            error=generation.ABANDONED + "billed, no usable output")
    assert generation.spend("s001")["spent"] == 0.60


def test_an_unrecorded_outcome_is_money_at_risk_not_money_ignored(scene):
    """The reachable half. The provider was called, it never answered, and
    nothing in the system knows whether it billed."""
    director, _, sb, render_dir, calls = scene
    _strand(director, sb, render_dir, calls)

    s = generation.spend("s011")
    assert s["at_risk"] == 0.60, "money that may have gone reported as nothing"
    assert s["at_risk_attempts"] == 1
    assert s["spent"] == 0.0, "an unrecorded outcome was reported as billed"
    assert s["spend_is_certain"] is False
    assert "at risk" in s["summary"]


def test_abandoning_the_attempt_does_not_settle_the_bill(scene):
    """Closing the attempt is what unblocks the beat. It answers nothing about
    the money, so the exposure must survive it."""
    director, _, sb, render_dir, calls = scene
    _strand(director, sb, render_dir, calls)
    stuck = [a for a in generation.for_shot("s011", "s011.01")
             if a.status == "running"][0]

    generation.abandon("s011", stuck.id, "checked the dashboard, no answer")

    s = generation.spend("s011")
    assert s["at_risk"] == 0.60, "closing the attempt made the exposure vanish"
    assert s["at_risk_attempts"] == 1
    assert s["spent"] == 0.0


def test_at_risk_money_is_never_folded_into_the_billed_total(scene):
    """§6.1: spent is what has ACTUALLY been billed. The retry that follows an
    abandon is billed; the abandoned attempt is not known to be. One number
    covering both would be accurate about neither."""
    director, _, sb, render_dir, calls = scene
    _strand(director, sb, render_dir, calls)
    stuck = [a for a in generation.for_shot("s011", "s011.01")
             if a.status == "running"][0]
    generation.abandon("s011", stuck.id, "no answer from the provider")

    director.compile_coverage(director.load_plan("s011"), sb, render_dir,
                              log=lambda m: None, skip_existing=False)
    assert calls["paid"] == 2

    s = generation.spend("s011")
    assert s["spent"] == 0.60, "the certain total absorbed the uncertain one"
    assert s["at_risk"] == 0.60
    assert s["paid_attempts"] == 1
    assert s["at_risk_attempts"] == 1


def test_the_price_is_recorded_before_the_provider_is_called(scene):
    """Without it the at-risk figure is $0.00, which is the original defect
    wearing a new key. After a crash there is no later moment to record it."""
    director, _, sb, render_dir, calls = scene
    _strand(director, sb, render_dir, calls)
    rows = generation.for_shot("s011", "s011.01")
    assert rows[0].estimated_cost == director.PAID_CLIP_COST


def test_a_running_attempt_is_money_at_risk():
    """Killed mid-generation looks exactly like live, and nothing here can tell
    them apart -- begin() already refuses a second attempt on that basis. The
    money is reported on the same footing."""
    _begin(idempotency_key="k1", estimated_cost=0.60)
    s = generation.spend("s001")
    assert s["spent"] == 0.0
    assert s["at_risk"] == 0.60


def test_an_ordinary_failure_is_not_money_at_risk():
    """fail() is reserved for failures the module can support as unbilled --
    which is the entire reason in_doubt() exists beside it. Reporting every
    failure as exposure would make the figure mean nothing."""
    att, _ = _begin(idempotency_key="k1", estimated_cost=0.60)
    generation.fail("s001", att.id, "rejected before dispatch")
    s = generation.spend("s001")
    assert s["at_risk"] == 0.0
    assert s["at_risk_attempts"] == 0
    assert s["spend_is_certain"] is True


def test_a_legacy_abandoned_record_is_still_money_at_risk():
    """Written before outcome_unknown existed. The marker abandon() leaves in
    the reason is all such a row has, and the money is no less at risk for it."""
    _record("s001", status=generation.FAILED, paid=True, estimated_cost=0.60,
            error=generation.ABANDONED + "resolved by hand months ago")
    assert generation.spend("s001")["at_risk"] == 0.60


def test_a_legacy_abandoned_record_can_still_be_abandoned_again():
    """Recognised for reporting and rejected for recovery is not a pair a
    ledger may hold.

    ``abandon()`` sends outcome_unknown through _finish, and _finish treats a
    terminal call as a replay only when EVERY field already matches. A row
    abandoned before that field existed reconstructs it as False, so a repeated
    abandon -- a double-click, a retried POST -- read as a conflicting claim
    about a finished attempt and answered 409 where it used to be a no-op. The
    migration on read is what keeps the two shapes identical.
    """
    _record("s001", status=generation.FAILED, paid=True, estimated_cost=0.60,
            error=generation.ABANDONED + "checked the dashboard, no answer")

    again = generation.abandon("s001", "s001.01.a1", "checked the dashboard, no answer")

    assert again is not None and again.status == generation.FAILED
    assert generation.spend("s001")["at_risk"] == 0.60


def test_a_recorded_failure_still_cannot_be_abandoned_over():
    """The migration must not soften the guard it rides on: an ordinary
    failure is a different claim, and abandon() must still refuse it."""
    att, _ = _begin(idempotency_key="k1")
    generation.fail("s001", att.id, "provider failed")
    with pytest.raises(generation.TerminalConflict):
        generation.abandon("s001", att.id, "human abandoned")


def test_a_free_attempt_is_never_money_at_risk():
    """Tier A and B are local and cost nothing. An unfinished free render is a
    render problem, not a billing one."""
    _begin(idempotency_key="k1", paid=False, estimated_cost=0.60)
    s = generation.spend("s001")
    assert s["at_risk"] == 0.0
    assert s["spent"] == 0.0


# --- S8-01 / TG-S4-06: a malformed ledger is not a settled zero -------------------
#
# `{}` is valid JSON, and it used to yield an empty attempt list -- which is
# exactly what "this beat never generated anything" looks like. That was already
# wrong; once spend() started answering spend_is_certain it became worse than the
# defect this slice set out to fix, because the report stopped merely omitting
# the money and started ASSERTING there was none, on a file nobody could read.
#
# Reachable: ledgers live on persistent storage, and a file truncated by a failed
# write, hand-repaired, or half-migrated is syntactically valid JSON.
#
# TG-S4-06 recorded these shapes raising incidental AttributeError/TypeError and
# was judged not a spend defect because it still failed closed. It is one now, so
# its core is closed here rather than in the slice item queued after this.

def _write_raw(beat_id: str, doc) -> None:
    from backend import atomic
    p = generation.ledger_path(beat_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    atomic.write_json(p, doc)


def _good_row(**over) -> dict:
    row = {"id": "s001.01.a1", "shot_id": "s001.01", "beat_id": "s001",
           "attempt": 1, "status": "succeeded", "paid": True, "cost": 0.60,
           "output": "a.mp4"}
    row.update(over)
    return row


MALFORMED = {
    "an empty object": {},
    "a header with no attempts": {"beat_id": "s001"},
    "attempts is not a list": {"beat_id": "s001", "attempts": {"a": 1}},
    "attempts is a string": {"beat_id": "s001", "attempts": "[]"},
    "the document is a list": [],
    "the document is a number": 3,
    "a row is not an object": {"beat_id": "s001", "attempts": ["s001.01.a1"]},
    "a row has no id": {"beat_id": "s001",
                        "attempts": [{k: v for k, v in _good_row().items()
                                      if k != "id"}]},
    "a row has no shot_id": {"beat_id": "s001",
                             "attempts": [{k: v for k, v in _good_row().items()
                                           if k != "shot_id"}]},
    "cost is a string": {"beat_id": "s001", "attempts": [_good_row(cost="0.60")]},
    "cost is negative": {"beat_id": "s001", "attempts": [_good_row(cost=-0.60)]},
    "cost is NaN": {"beat_id": "s001",
                    "attempts": [_good_row(cost=float("nan"))]},
    "estimated_cost is a string": {"beat_id": "s001",
                                   "attempts": [_good_row(estimated_cost="1")]},
    "paid is a string": {"beat_id": "s001", "attempts": [_good_row(paid="yes")]},
    "attempt is a bool": {"beat_id": "s001", "attempts": [_good_row(attempt=True)]},
    "attempt is zero": {"beat_id": "s001", "attempts": [_good_row(attempt=0)]},
    "status is unrecognised": {"beat_id": "s001",
                               "attempts": [_good_row(status="cancelled")]},
    "the ledger names another beat": {"beat_id": "s999",
                                      "attempts": [_good_row()]},
    "a row names another beat": {"beat_id": "s001",
                                 "attempts": [_good_row(beat_id="s999")]},

    # --- TG-S4-06: shapes the gate already rejects that nothing pinned --------
    #
    # Each one is a field the money code reads and a JSON type it must not hold.
    # They were reachable through the same routes as the cases above -- a
    # truncated write, a hand repair, a half-finished migration -- and the gate
    # covering them today is not the same as a test noticing if it stopped.
    "attempts is null": {"beat_id": "s001", "attempts": None},
    # `beat_id` and `attempt` are the two required keys the sweep never removed;
    # without them a row cannot be reconciled against a bill at all.
    "a row has no beat_id": {"beat_id": "s001",
                             "attempts": [{k: v for k, v in _good_row().items()
                                           if k != "beat_id"}]},
    "a row has no attempt number": {"beat_id": "s001",
                                    "attempts": [{k: v for k, v in _good_row().items()
                                                  if k != "attempt"}]},
    # `billed()` reads `bool(a.output)`, so a non-string here decides whether a
    # bought clip counts -- 0 and "" are both falsy and mean different things.
    "output is a number": {"beat_id": "s001", "attempts": [_good_row(output=123)]},
    "output is null": {"beat_id": "s001", "attempts": [_good_row(output=None)]},
    # load_attempts calls .startswith on this to migrate legacy abandons.
    "error is null": {"beat_id": "s001", "attempts": [_good_row(error=None)]},
    # Distinct from "cancelled": null takes the type check, not the vocabulary
    # check, and only one of the two branches would survive a partial revert.
    "status is null": {"beat_id": "s001", "attempts": [_good_row(status=None)]},
    "an id is null": {"beat_id": "s001", "attempts": [_good_row(id=None)]},
    # `at_risk()` reads outcome_unknown directly; any truthy string would pass
    # for True and any empty one for False, which is not a decision about money.
    "outcome_unknown is a string": {"beat_id": "s001",
                                    "attempts": [_good_row(outcome_unknown="yes")]},
    # Infinity survives json.loads exactly as NaN does, and poisons a total the
    # same way -- but through the isfinite branch rather than the NaN one.
    "cost is Infinity": {"beat_id": "s001",
                         "attempts": [_good_row(cost=float("inf"))]},
    "estimated_cost is negative": {"beat_id": "s001",
                                   "attempts": [_good_row(estimated_cost=-1.0)]},
    "estimated_cost is NaN": {"beat_id": "s001",
                              "attempts": [_good_row(estimated_cost=float("nan"))]},
    "attempt is negative": {"beat_id": "s001", "attempts": [_good_row(attempt=-3)]},
}

# The shape the audit named in its own words: right syntax, wrong types, and a
# cost that would reach a sum. Used wherever one representative case is enough.
STRUCTURALLY_INVALID = {"beat_id": "s001", "attempts": [_good_row(cost="0.60")]}


@pytest.mark.parametrize("case", sorted(MALFORMED))
def test_a_malformed_ledger_is_unreadable_not_empty(case):
    _write_raw("s001", MALFORMED[case])
    with pytest.raises(generation.LedgerUnreadable):
        generation.load_attempts("s001")


@pytest.mark.parametrize("case", sorted(MALFORMED))
def test_a_malformed_ledger_never_reports_a_certain_zero(case):
    """The point of the whole slice, applied to the read path. Whatever this
    file is, it is not evidence that nothing was billed."""
    _write_raw("s001", MALFORMED[case])
    with pytest.raises(generation.LedgerUnreadable):
        generation.spend("s001")


def test_a_malformed_ledger_refuses_to_spend_rather_than_reporting_nothing():
    """And it must reach the guard that matters: begin() cannot open a paid
    attempt against a ledger it could not read, because it cannot know whether
    one is already running."""
    _write_raw("s001", {})
    with pytest.raises(generation.LedgerUnreadable):
        _begin(idempotency_key="k1")
    assert generation.ledger_path("s001").read_text(encoding="utf-8").strip() == "{}", \
        "the unreadable ledger was overwritten"


def test_a_well_formed_empty_ledger_is_still_an_empty_lineage():
    """Failing closed must not mean refusing a beat that genuinely has no
    history. An attempts list that is present and empty is a fact, not a gap."""
    _write_raw("s001", {"beat_id": "s001", "attempts": []})
    assert generation.load_attempts("s001") == []
    s = generation.spend("s001")
    assert s["spent"] == 0.0 and s["spend_is_certain"] is True


def test_an_unknown_field_from_a_newer_writer_is_ignored_not_fatal():
    """Forward compatibility is not the same as tolerating nonsense: a field
    this version does not know is dropped, while a field it knows and cannot
    trust stops the read."""
    _write_raw("s001", {"beat_id": "s001",
                        "attempts": [_good_row(settled_by="a later slice")]})
    rows = generation.load_attempts("s001")
    assert len(rows) == 1 and rows[0].cost == 0.60


# --- TG-S4-06: the promised error contract, and the gate's own blind spot ---------
#
# The audit's complaint was never that structurally invalid ledgers failed open
# -- it said explicitly that they did not. It was that callers are promised
# LedgerUnreadable and used to get an incidental AttributeError or TypeError, so
# the contract held for one kind of corruption and not another.
#
# The schema gate closes that. What the tests above do not cover is who it holds
# FOR: they exercise load_attempts() and spend(), and every other public entry
# point reaches the same file by its own route. `begin()` is the one that decides
# whether money may be spent, and `succeed`/`fail`/`abandon`/`in_doubt` are the
# ones that record what a provider did. A gate that covered the read path and
# not those would be a contract that holds for reporting and not for spending.

# Every public entry point that touches the ledger, with an argument list that
# reaches the file. A key or signature must be non-empty: both functions answer
# without reading when they are blank, which is correct and not what is measured
# here.
LEDGER_CALLERS = {
    "load_attempts": lambda: generation.load_attempts("s001"),
    "for_shot": lambda: generation.for_shot("s001", "s001.01"),
    "history": lambda: generation.history("s001", "s001.01"),
    "find_by_key": lambda: generation.find_by_key("s001", "req-1"),
    "reusable": lambda: generation.reusable("s001", "s001.01", "sig-a"),
    "spend": lambda: generation.spend("s001"),
    "begin": lambda: _begin(idempotency_key="k1"),
    "succeed": lambda: generation.succeed("s001", "s001.01.a1", "a.mp4", cost=0.60),
    "fail": lambda: generation.fail("s001", "s001.01.a1", "boom"),
    "in_doubt": lambda: generation.in_doubt("s001", "s001.01.a1", "no answer"),
    "abandon": lambda: generation.abandon("s001", "s001.01.a1", "wrote it off"),
}


@pytest.mark.parametrize("caller", sorted(LEDGER_CALLERS))
def test_every_entry_point_answers_a_broken_ledger_with_the_promised_error(caller):
    """One exception type, whoever asks. That is the whole of TG-S4-06.

    An incidental TypeError out of `begin()` is not merely untidy: callers that
    catch LedgerUnreadable -- the two routes and the plan payload -- would let it
    through as a 500, and a 500 is what a client retries.
    """
    _write_raw("s001", STRUCTURALLY_INVALID)
    with pytest.raises(generation.LedgerUnreadable):
        LEDGER_CALLERS[caller]()


@pytest.mark.parametrize("caller", sorted(LEDGER_CALLERS))
def test_no_entry_point_overwrites_the_ledger_it_could_not_read(caller):
    """Failing closed is only half of it; the bytes are the evidence.

    `begin()` and the `_finish` family all save the list they loaded. If any of
    them reached its write with a partially-parsed list, the unreadable file
    would be replaced by a tidy one that had quietly dropped the rows it could
    not understand -- and the atomic write would preserve that perfectly.
    """
    _write_raw("s001", STRUCTURALLY_INVALID)
    before = generation.ledger_path("s001").read_bytes()
    with pytest.raises(generation.LedgerUnreadable):
        LEDGER_CALLERS[caller]()
    assert generation.ledger_path("s001").read_bytes() == before


def test_the_gate_validates_every_field_an_attempt_can_carry():
    """The gate's own blind spot, and the reason this is a test and not a fix.

    `_row` validates a field only if `_SHAPE` names it, and then hands whatever
    is left to `GenerationAttempt(**...)`. So the gate is exactly as complete as
    that table, and nothing makes the table follow the dataclass: add a field to
    `GenerationAttempt` and forget the table, and it arrives from JSON with no
    type check at all. If it is a money field -- and the last two added were
    `estimated_cost` and `outcome_unknown`, both money fields -- the ledger goes
    back to reporting a total it cannot support.

    This is a structural test on purpose. No fixture can exercise a field that
    does not exist yet; the coupling is the only thing that can be asserted
    before the mistake is made.
    """
    fields = set(generation.GenerationAttempt.__dataclass_fields__)
    unvalidated = fields - set(generation._SHAPE)
    assert not unvalidated, (
        f"GenerationAttempt carries {sorted(unvalidated)}, which _SHAPE does not "
        f"validate. Values for these reach the dataclass straight from JSON, so "
        f"a string lands where a float is expected and the first total that "
        f"touches it is wrong or raises.")

    stale = set(generation._SHAPE) - fields
    assert not stale, (
        f"_SHAPE validates {sorted(stale)}, which is not a field of "
        f"GenerationAttempt. _row drops unknown keys before construction, so "
        f"these entries check nothing and read as coverage that is not there.")


def test_the_two_shapes_the_gate_lets_through_still_fail_closed():
    """Recorded, not fixed: the gate is not exhaustive and does not claim to be.

    Two JSON-valid shapes load without complaint -- two rows sharing one id, and
    a row with an empty id. Both were checked against the money path rather than
    assumed:

    * duplicate ids OVER-report. `spend()` counts both rows, so one $0.60 clip
      reads as $1.20, and `_finish` refuses the second as a terminal conflict.
    * a running duplicate makes `begin()` answer `in_flight`, which refuses to
      spend.

    Over-reporting exposure and refusing to spend are both the safe direction, so
    tightening the gate here would be a behaviour change made during a test-gap
    round for no money gained. It is pinned instead, so that if either shape ever
    starts under-reporting, this test is what says so.
    """
    row = _good_row(cost=0.60)
    _write_raw("s001", {"beat_id": "s001", "attempts": [dict(row), dict(row)]})

    s = generation.spend("s001")
    assert s["spent"] == 1.20, "a duplicated row started UNDER-reporting"
    assert s["paid_attempts"] == 2
    with pytest.raises(generation.TerminalConflict):
        generation.fail("s001", row["id"], "a late callback")

    running = _good_row(status="running", cost=0.0, output="",
                        estimated_cost=0.60, signature="sig-a")
    _write_raw("s001", {"beat_id": "s001",
                        "attempts": [dict(running), dict(running)]})
    _, how = _begin(idempotency_key="")
    assert how == "in_flight", "a duplicated running attempt stopped blocking a re-buy"
    assert generation.spend("s001")["at_risk"] == 1.20


# --- S8-01: and no caller may read the total without the risk ---------------------

def test_the_lineage_api_reports_what_is_at_risk_beside_the_total(api):
    client, director, sb, render_dir, calls = api
    _strand(director, sb, render_dir, calls)

    body = client.get("/api/generation/s011").json()
    assert body["spend"]["spent"] == 0.0
    assert body["spend"]["at_risk"] == 0.60
    assert body["at_risk"] == 0.60, "a client reading the total alone sees nothing"
    assert "at risk" in body["spend"]["summary"]


def test_abandoning_answers_with_the_money_it_put_at_risk(api):
    """The act that creates the exposure is the one that must report it. A reply
    carrying only the billed total tells the human who just wrote off a possible
    charge that nothing happened."""
    client, director, sb, render_dir, calls = api
    _strand(director, sb, render_dir, calls)
    stuck = client.get("/api/generation/s011").json()["unresolved"][0]

    body = client.post("/api/generation/s011/" + stuck["id"] + "/abandon",
                       json={"reason": "no answer from the provider"}).json()
    assert body["ok"] is True
    assert body["at_risk"] == 0.60
    assert body["spend"]["spent"] == 0.0


def test_the_plan_payload_reports_the_money_at_risk(api):
    """A stuck paid attempt is money, not just a blocked beat, and the plan
    screen is where the block is already reported."""
    client, director, sb, render_dir, calls = api
    _strand(director, sb, render_dir, calls)
    plan = client.get("/api/director/plan/s011").json()["plan"]
    assert plan["at_risk"] == 0.60
    assert plan["spend"]["spent"] == 0.0


def test_an_unreadable_ledger_states_the_exposure_is_unknown(api):
    """The path where the exposure is LEAST known must not be the one that
    reports nothing at stake.

    Omitting the keys is not neutral: a client reads undefined, and `?? 0`
    renders $0.00 -- this PR's own defect, moved to the error path. So the
    figures are present and null, and the summary says what is wrong instead of
    substituting a number for it.
    """
    client, director, sb, render_dir, calls = api
    _strand(director, sb, render_dir, calls)
    generation.ledger_path("s011").write_text("{not json", encoding="utf-8")

    plan = client.get("/api/director/plan/s011").json()["plan"]
    assert plan["at_risk"] is None, "an unreadable ledger reported no exposure"
    assert plan["spend"]["spent"] is None
    assert plan["spend"]["at_risk"] is None
    assert plan["spend"]["spend_is_certain"] is False
    assert "unknown" in plan["spend"]["summary"]
    assert plan["lineage_error"]


def test_no_caller_can_read_the_billed_total_without_the_risk():
    """A new field is only a fix if a caller cannot quietly ignore it.

    Every module that reads a spend total must also name the at-risk figure.
    This is a source check on purpose: the failure it guards is a FUTURE caller
    that renders `spent` on its own, and no behavioural test can see a caller
    that has not been written yet.
    """
    root = Path(__file__).resolve().parent.parent
    files = list((root / "backend").rglob("*.py")) + list(root.glob("*.py"))
    src = root / "frontend" / "src"
    if src.is_dir():
        files += [p for p in src.rglob("*.ts*") if ".test." not in p.name]

    def code(text: str) -> str:
        # Comment lines do not count. A file that mentions at_risk only while
        # explaining something satisfies the letter of this guard and none of
        # its point.
        return "\n".join(ln for ln in text.splitlines()
                         if not ln.lstrip().startswith(("#", "//", "*", "/*")))

    offenders = []
    for p in files:
        text = code(p.read_text(encoding="utf-8", errors="replace"))
        reads_total = ("generation.spend(" in text or '["spent"]' in text
                       or "['spent']" in text or ".spent" in text)
        if reads_total and "at_risk" not in text:
            offenders.append(str(p.relative_to(root)))

    assert not offenders, (
        "these read a spend total without the at-risk figure beside it, which "
        f"is how a charge reports $0.00 again: {offenders}")
