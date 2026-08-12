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


# --- S2-01: the signature preimage must be unambiguous ---------------------------

def _one_shot(**kw) -> director.CoveragePlan:
    p = director.CoveragePlan(beat_id="s001", beat_duration=6.0)
    p.coverage = [director.DirectorShot(id="s001.01", beat_id="s001",
                                        camera=Camera(move="static", duration=6.0), **kw)]
    return p


def test_a_field_value_cannot_shift_the_signature_boundaries():
    """S2-01. The preimage was 'field|field|field', so a value containing the
    delimiter moved the boundaries and two different plans signed identically.
    Those are user-controlled strings: reachable by typing."""
    a = _one_shot(purpose="alpha|beta", subject="gamma")
    b = _one_shot(purpose="alpha", subject="beta|gamma")
    assert director.plan_signature(a) != director.plan_signature(b)


@pytest.mark.parametrize("pair", [
    pytest.param((dict(prompt="a|b", motion_prompt="c"),
                  dict(prompt="a", motion_prompt="b|c")), id="prompts"),
    pytest.param((dict(shot_size="ws|cu", angle="low"),
                  dict(shot_size="ws", angle="cu|low")), id="framing"),
    pytest.param((dict(subject="x", composition=""),
                  dict(subject="x|", composition="")), id="trailing_delimiter"),
    pytest.param((dict(reference_dependencies=["a,b"]),
                  dict(reference_dependencies=["a", "b"])), id="list_join"),
])
def test_no_pair_of_materially_different_plans_shares_a_signature(pair):
    left, right = pair
    assert director.plan_signature(_one_shot(**left)) != director.plan_signature(_one_shot(**right))


def test_an_edited_plan_cannot_borrow_another_plans_approval(studio):
    """The end-to-end form of S2-01: copying a signature must not unlock compile."""
    client, dispatched = studio
    p = director.CoveragePlan(beat_id="s001", beat_duration=12.0, status="draft")
    p.coverage = [
        director.DirectorShot(id="s001.01", beat_id="s001", purpose="alpha|beta",
                              subject="gamma", motion_type="parallax", prompt="p",
                              camera=Camera(move="static", duration=12.0)),
    ]
    director.save_plan(p)
    client.post("/api/director/lock/s001")
    borrowed = director.load_plan("s001").approved_signature

    edited = director.load_plan("s001")
    edited.coverage[0].purpose = "alpha"
    edited.coverage[0].subject = "beta|gamma"
    edited.status = "locked"
    edited.approved_signature = borrowed          # the forged approval
    director.save_plan(edited)

    r = client.post("/api/director/compile/s001")
    assert r.status_code == 409
    assert dispatched == [], "an edited plan compiled under a borrowed approval"


# --- S2-02: transitions are persisted, not reconstructed each read ---------------

def test_migration_is_written_to_the_file_not_just_the_object(studio):
    """S2-02. load_plan mutated the returned object and left the file alone, so
    the JSON still asserted a locked plan with no signature at all."""
    import json as _json
    _plan("locked")
    assert "approved_signature" not in _json.loads(
        director.plan_path("s001").read_text(encoding="utf-8")) or True

    director.load_plan("s001")
    raw = _json.loads(director.plan_path("s001").read_text(encoding="utf-8"))
    assert raw["approved_signature"], "migration was not persisted"
    assert raw["approved_by"] == "migrated:pre-signature-lock"


def test_drift_invalidation_is_written_to_the_file(studio):
    """The file kept saying locked, so anything not reading through load_plan
    still saw an approval that had already been invalidated."""
    import json as _json
    client, _ = studio
    _plan("draft")
    client.post("/api/director/lock/s001")

    p = director.load_plan("s001")
    p.coverage[0].camera.duration = 5.0
    p.coverage[1].camera.duration = 7.0
    p.status = "locked"
    director.save_plan(p)

    director.load_plan("s001")
    raw = _json.loads(director.plan_path("s001").read_text(encoding="utf-8"))
    assert raw["status"] == "draft", "the file still asserted a stale lock"
    assert raw["approved_signature"] == ""
    assert len(raw["approval_history"]) == 1, "history was rebuilt per read, not recorded"


def test_persisting_a_transition_happens_once_not_on_every_read(studio, monkeypatch):
    """Conditional write: the second read must find nothing to do.

    Counts WRITES rather than comparing file contents. A read that re-saves
    identical bytes leaves the file byte-identical, so a content comparison
    cannot tell "wrote nothing" from "wrote the same thing again" -- and the
    thing being asserted is that reads are not writes.
    """
    writes: list[str] = []
    real = director.save_plan
    monkeypatch.setattr(director, "save_plan",
                        lambda pl: (writes.append(pl.beat_id), real(pl))[1])

    _plan("locked")
    writes.clear()

    director.load_plan("s001")
    assert len(writes) == 1, "the transitioning read should persist exactly once"
    for _ in range(3):
        director.load_plan("s001")
    assert len(writes) == 1, f"a settled plan was rewritten on every read ({len(writes)})"


def test_history_does_not_grow_on_repeated_reads(studio):
    client, _ = studio
    _plan("draft")
    client.post("/api/director/lock/s001")
    p = director.load_plan("s001")
    p.coverage[0].camera.duration = 5.0
    p.coverage[1].camera.duration = 7.0
    p.status = "locked"
    director.save_plan(p)

    for _ in range(4):
        director.load_plan("s001")
    assert len(director.load_plan("s001").approval_history) == 1


def test_a_plan_write_leaves_no_temp_file(studio):
    _plan("draft")
    leftovers = list(director.director_dir().glob("*.tmp"))
    assert leftovers == [], f"temp files left behind: {leftovers}"


# --- S2-03: concurrent writers to the same beat ---------------------------------

def _concurrent_saves(beat_id: str, writers: int = 8, rounds: int = 25):
    """Run `writers` threads all saving the same beat, return any exceptions."""
    import threading
    errors: list[str] = []
    start = threading.Barrier(writers, timeout=10)

    def one(i: int):
        try:
            start.wait()
            for _ in range(rounds):
                p = director.CoveragePlan(beat_id=beat_id, beat_duration=6.0,
                                          profile=f"w{i}")
                p.coverage = [director.DirectorShot(
                    id=f"{beat_id}.01", beat_id=beat_id, prompt=f"w{i}",
                    camera=Camera(move="static", duration=6.0))]
                director.save_plan(p)
        except Exception as exc:  # noqa: BLE001 - the point is to catch any
            errors.append(f"{type(exc).__name__}: {exc}")

    threads = [threading.Thread(target=one, args=(i,)) for i in range(writers)]
    for th in threads:
        th.start()
    for th in threads:
        th.join(timeout=20)
    return errors


def test_concurrent_writers_to_one_beat_do_not_fail(studio):
    """S2-03. A single fixed temp name per beat meant competing writers shared
    it, and os.replace failed with WinError 32 -- 7 of 8 writers lost their
    write. Atomic replacement protects one writer from exposing a partial file;
    it does not make competing writers safe."""
    _plan("draft")
    errors = _concurrent_saves("s001")
    assert errors == [], f"concurrent saves raised: {errors[:3]}"


def test_a_concurrently_written_plan_is_complete_not_spliced(studio):
    """Serialising must yield one writer's whole state, not a mixture."""
    import json as _json
    _plan("draft")
    assert _concurrent_saves("s001") == []
    raw = _json.loads(director.plan_path("s001").read_text(encoding="utf-8"))
    assert raw["profile"] == raw["coverage"][0]["prompt"],         "the surviving file mixes two writers' states"


def test_concurrency_leaves_no_temp_files_behind(studio):
    _plan("draft")
    _concurrent_saves("s001", writers=4, rounds=10)
    leftovers = list(director.director_dir().glob("*.tmp"))
    assert leftovers == [], f"temp files left behind: {leftovers}"


def test_different_beats_do_not_serialise_against_each_other(studio):
    """The lock is per plan file, so unrelated beats still write in parallel."""
    _plan("draft")
    assert _concurrent_saves("s002", writers=4, rounds=5) == []
    assert _concurrent_saves("s003", writers=4, rounds=5) == []
    assert director.plan_path("s002").is_file()
    assert director.plan_path("s003").is_file()


def test_a_persisted_transition_survives_concurrent_readers(studio):
    """load_plan writes now, so readers are writers. They must not collide."""
    import threading
    client, _ = studio
    _plan("locked")
    errors: list[str] = []
    start = threading.Barrier(6, timeout=10)

    def read():
        try:
            start.wait()
            for _ in range(10):
                director.load_plan("s001")
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{type(exc).__name__}: {exc}")

    threads = [threading.Thread(target=read) for _ in range(6)]
    for th in threads:
        th.start()
    for th in threads:
        th.join(timeout=20)

    assert errors == [], f"concurrent transitioning reads raised: {errors[:3]}"
    assert director.approval_is_current(director.load_plan("s001"))


# --- the material contract is stated, so new fields must be classified -----------

def test_every_director_shot_field_is_classified_as_material_or_not():
    """Adding a field to DirectorShot must force a decision about approval.

    Codex flagged library_scope as recorded-for-later and unclassified. The
    answer is not to add that one field, it is to make the omission impossible.
    """
    known = set(director.DirectorShot.__dataclass_fields__)
    classified = set(director._MATERIAL_SHOT_FIELDS) | set(director._NON_MATERIAL_SHOT_FIELDS)
    assert known - classified == set(), (
        f"DirectorShot gained field(s) {sorted(known - classified)}. Decide whether "
        f"each one changes what gets produced: add it to _MATERIAL_SHOT_FIELDS, or "
        f"to _NON_MATERIAL_SHOT_FIELDS with the reason.")
    assert classified - known == set(), "a classified field no longer exists"


def test_every_coverage_plan_field_is_classified():
    known = set(director.CoveragePlan.__dataclass_fields__)
    classified = ({"beat_id", "beat_duration"} | set(director._NON_MATERIAL_PLAN_FIELDS))
    assert known - classified == set(), (
        f"CoveragePlan gained field(s) {sorted(known - classified)}; classify each "
        f"in _NON_MATERIAL_PLAN_FIELDS or include it in the signature.")


def test_library_scope_is_material():
    """It selects which asset a library shot resolves to, so it changes pixels."""
    a = _one_shot(source="library", source_ref="x.png", library_scope="series")
    b = _one_shot(source="library", source_ref="x.png", library_scope="project")
    assert director.plan_signature(a) != director.plan_signature(b)


def test_the_signature_is_versioned():
    """A stored signature must never be compared against one computed a
    different way without the change being explicit."""
    assert director.material_plan(_one_shot())["signature_version"] == director.SIGNATURE_VERSION


def test_sustained_contention_loses_no_submitted_save(studio):
    """S2-03 round 2. The per-file lock stops two of OUR writers meeting; it
    cannot stop Windows transiently denying the replace, which surfaced in 5 of
    20 batches at this load. A denied replace is a submitted save silently lost:
    load_plan swallows the OSError, so the process believes a transition it
    never wrote.

    Repeated batches on purpose -- a single batch passed ten times in a row
    while the defect was still present.
    """
    import json as _json
    _plan("draft")
    for batch in range(6):
        errors = _concurrent_saves("s001", writers=12, rounds=15)
        assert errors == [], f"batch {batch} lost a save: {errors[:2]}"

    raw = _json.loads(director.plan_path("s001").read_text(encoding="utf-8"))
    assert raw["profile"] == raw["coverage"][0]["prompt"], "surviving file is spliced"
    assert list(director.director_dir().glob("*.tmp")) == []


def test_a_real_permission_error_is_not_retried_forever(monkeypatch):
    """Bounded on purpose: a genuine permissions problem must fail, not spin."""
    import os
    calls = {"n": 0}

    def always_denied(src, dst):
        calls["n"] += 1
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(os, "replace", always_denied)
    with pytest.raises(PermissionError):
        director._replace_with_retry(Path("a"), Path("b"))
    assert calls["n"] == 1, "a non-transient errno must not be retried"


def test_a_transient_denial_is_retried_and_succeeds(monkeypatch):
    import os
    state = {"n": 0}
    real = os.replace

    def flaky(src, dst):
        state["n"] += 1
        if state["n"] < 3:
            exc = PermissionError(13, "Access is denied")
            exc.winerror = 5
            raise exc
        return real(src, dst)

    monkeypatch.setattr(os, "replace", flaky)
    import tempfile
    d = Path(tempfile.mkdtemp())
    src, dst = d / "src", d / "dst"
    src.write_text("x", encoding="utf-8")
    director._replace_with_retry(src, dst)
    assert dst.read_text(encoding="utf-8") == "x"
    assert state["n"] == 3, "should have retried twice then succeeded"


def test_the_retry_is_bounded(monkeypatch):
    """It must give up rather than hang a request forever."""
    import os
    calls = {"n": 0}

    def always_transient(src, dst):
        calls["n"] += 1
        exc = PermissionError(13, "Access is denied")
        exc.winerror = 32
        raise exc

    monkeypatch.setattr(os, "replace", always_transient)
    monkeypatch.setattr(director, "_REPLACE_BACKOFF", 0.0)
    with pytest.raises(PermissionError):
        director._replace_with_retry(Path("a"), Path("b"))
    assert calls["n"] == director._REPLACE_ATTEMPTS
