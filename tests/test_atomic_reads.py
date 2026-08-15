"""Reading a record is as exposed to a concurrent replace as writing one.

``backend.atomic`` already protected writers from each other: a lock per
destination, a unique temp per write, bounded retry on the replace. Nothing
protected a *reader*. On Windows, opening a file while another thread is mid
``os.replace`` on it is denied, and the denial reached the caller — which is the
reader-side form of the failure the module's own docstring names. A submitted
write silently lost is bad because the caller believes something it never
persisted; a denied read is bad because the caller is told a record it has every
right to is unreadable.

The denials here are injected rather than raced for. The race reproduces about
nine runs in ten under full-suite load, which is enough to catch a regression and
not enough to *assert* one; injection makes the same property deterministic. The
injected exception is the real one: ``PermissionError`` with ``errno == 13`` and
``winerror`` unset, because ``open`` goes through the C runtime and only
``os.replace`` carries a Win32 error code.
"""

from __future__ import annotations

import errno
import json
import sys
import threading
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
for _m in ("anthropic", "fal_client", "elevenlabs"):
    sys.modules.setdefault(_m, types.ModuleType(_m))

from backend import atomic, config, director, exports, generation, slots  # noqa: E402
from backend.manifest import Camera, Shot, Storyboard  # noqa: E402


def _denial(path: Path) -> PermissionError:
    """Exactly what a Windows open() denied by a concurrent replace raises.

    Three arguments, not four: the C runtime has already mapped both
    ERROR_ACCESS_DENIED and ERROR_SHARING_VIOLATION onto EACCES and set no
    winerror. A retry predicate keyed on winerror sees None here and gives up,
    which is the whole reason this helper is spelled out rather than mocked.
    """
    return PermissionError(errno.EACCES, "Permission denied", str(path))


@pytest.fixture
def deny(monkeypatch):
    """Make the next ``n`` reads of a path fail the way Windows fails them."""
    real = Path.read_text
    budget: dict[str, int] = {}

    def fake(self, *a, **k):
        if budget.get(str(self), 0) > 0:
            budget[str(self)] -= 1
            raise _denial(self)
        return real(self, *a, **k)

    monkeypatch.setattr(Path, "read_text", fake)

    def arm(path: Path, times: int = 1):
        budget[str(path)] = times

    return arm


@pytest.fixture(autouse=True)
def project(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "MANIFEST_PATH", tmp_path / "storyboard_manifest.json")
    return tmp_path


# --- the primitive ---------------------------------------------------------------

def test_a_transiently_denied_read_is_retried_not_surfaced(tmp_path, deny):
    """The defect, at the level it was fixed. One denial must not reach the caller."""
    p = tmp_path / "rec.json"
    atomic.write_json(p, {"n": 1})
    deny(p, times=1)
    assert atomic.read_json(p) == {"n": 1}


def test_the_retry_survives_a_denial_on_every_attempt_but_the_last(tmp_path, deny):
    """Bounded, and the bound is the write side's bound. Not one attempt fewer."""
    p = tmp_path / "rec.json"
    atomic.write_json(p, {"n": 2})
    deny(p, times=atomic._ATTEMPTS - 1)
    assert atomic.read_json(p) == {"n": 2}


def test_a_permanent_denial_still_fails_rather_than_spinning(tmp_path, deny):
    """Bounded the other way: a file we genuinely may not read must raise."""
    p = tmp_path / "rec.json"
    atomic.write_json(p, {"n": 3})
    deny(p, times=atomic._ATTEMPTS + 5)
    with pytest.raises(PermissionError):
        atomic.read_json(p)


def test_a_missing_record_is_an_answer_and_is_not_retried(tmp_path, monkeypatch):
    """ENOENT is not transient. Retrying it would make every absent record cost
    a second of backoff, and callers ask 'is there one?' constantly."""
    p = tmp_path / "gone.json"
    calls = []
    real = Path.read_text
    monkeypatch.setattr(Path, "read_text",
                        lambda self, *a, **k: (calls.append(1), real(self, *a, **k))[1])
    with pytest.raises(FileNotFoundError):
        atomic.read_json(p)
    assert len(calls) == 1, f"a missing record was retried {len(calls)} times"


def test_a_corrupt_record_raises_once_and_is_not_retried(tmp_path, monkeypatch):
    """Retrying a parse reads a corrupt file eight times and still fails. Worse,
    it would hide corruption behind a delay that looks like contention."""
    p = tmp_path / "bad.json"
    p.write_text("{not json", encoding="utf-8")
    calls = []
    real = Path.read_text
    monkeypatch.setattr(Path, "read_text",
                        lambda self, *a, **k: (calls.append(1), real(self, *a, **k))[1])
    with pytest.raises(ValueError):
        atomic.read_json(p)
    assert len(calls) == 1, f"a corrupt record was re-read {len(calls)} times"


# --- the lock half ---------------------------------------------------------------

def test_a_reader_waits_for_the_destination_lock(tmp_path):
    """The half that actually closes the in-process race.

    Retry alone would paper over the collision after the fact; taking the same
    lock the writer takes means our reader is never inside open() while our
    writer is inside os.replace.
    """
    p = tmp_path / "rec.json"
    atomic.write_json(p, {"n": 4})
    done = threading.Event()

    with atomic.lock_for(p):
        t = threading.Thread(target=lambda: (atomic.read_json(p), done.set()))
        t.start()
        assert not done.wait(0.4), "read_json read the record without taking its lock"
    t.join(timeout=5)
    assert done.is_set(), "the reader never completed after the lock was released"


def test_reading_one_record_does_not_block_reading_another(tmp_path):
    """Per path, like the write lock. Two projects' records must not queue."""
    a, b = tmp_path / "a.json", tmp_path / "b.json"
    atomic.write_json(a, {"n": 5})
    atomic.write_json(b, {"n": 6})
    done = threading.Event()

    with atomic.lock_for(a):
        t = threading.Thread(target=lambda: (atomic.read_json(b), done.set()))
        t.start()
        assert done.wait(5), "an unrelated record serialised against this one"
    t.join(timeout=5)


# --- every read site of an atomic record is routed through it --------------------
#
# These are the tests that fail if someone adds a fourth record, or reverts one
# of the three call sites to a bare read_text. They assert at the consumer, not
# at atomic.read_json, because a call site that is merely *reachable* proves
# nothing -- what matters is that load_plan/load_attempts/slots.load return their
# value through a denial rather than raising.

def _plan(status: str = "draft") -> director.CoveragePlan:
    p = director.CoveragePlan(beat_id="s001", beat_duration=6.0, status=status)
    p.coverage = [director.DirectorShot(id="s001.01", beat_id="s001", prompt="a",
                                        camera=Camera(move="static", duration=6.0))]
    director.save_plan(p)
    return p


def test_load_plan_survives_a_denied_read(deny):
    _plan("draft")
    deny(director.plan_path("s001"))
    plan = director.load_plan("s001")
    assert plan is not None and plan.beat_id == "s001"
    assert len(plan.coverage) == 1, "the plan came back without its coverage"


def test_load_attempts_survives_a_denied_read(deny):
    generation.begin(beat_id="s001", shot_id="s001.01", signature="sig",
                     idempotency_key="k1")
    deny(generation.ledger_path("s001"))
    attempts = generation.load_attempts("s001")
    assert [a.shot_id for a in attempts] == ["s001.01"]


def test_slots_load_survives_a_denied_read(deny):
    slots.save([slots.TimelineSlot(id="s001::beat", beat_id="s001",
                                   intended_duration=6.0)])
    deny(slots.slots_path())
    assert [s.id for s in slots.load()] == ["s001::beat"]


def test_load_snapshot_survives_a_denied_read(deny):
    """An export snapshot is the record that makes a deliverable verifiable.

    A denial reaching the caller here reports a snapshot that is present as
    unreadable, which downgrades a perfectly good frozen export to
    "provenance unavailable" — and the migration constraint says such an export
    is unverifiable. A transient Windows denial must not be able to manufacture
    that state.
    """
    sb = Storyboard(title="Frozen Cut", shots=[
        Shot(scene_id="s001", camera=Camera(move="static", duration=6.0))])
    exports.freeze(sb, version="v1", preset="h264")
    deny(exports.snapshot_path("v1"))
    snapshot = exports.load_snapshot("v1")
    assert snapshot["export_version"] == "v1"
    assert snapshot["frozen"]["storyboard"]["shots"][0]["scene_id"] == "s001"


def test_export_history_survives_a_denied_read(deny):
    """History is read-modify-write, so a denied read is also a lost append.

    Worse than the ordinary reader case: ``_append`` reads the file before
    writing it back, so a denial that reached the caller mid-append would either
    drop the row or — if the caller treated it as "no history yet" — overwrite
    every prior row with a single new one, which is the one thing an append-only
    record must never do.
    """
    exports.record("v1", "master", "succeeded", snapshot="sha256:abc")
    deny(exports.history_path())
    assert [r["version"] for r in exports.history()] == ["v1"]
    deny(exports.history_path())
    exports.record("v2", "master", "succeeded", snapshot="sha256:def")
    assert [r["version"] for r in exports.history()] == ["v1", "v2"]


def test_a_denied_ledger_read_is_still_refused_when_it_is_not_transient(deny):
    """Routing the read through atomic must not soften generation's fail-closed
    rule. An unreadable ledger is still not an empty one -- it just takes the
    bounded retry first."""
    generation.begin(beat_id="s002", shot_id="s002.01", signature="sig",
                     idempotency_key="k2")
    deny(generation.ledger_path("s002"), times=atomic._ATTEMPTS + 5)
    with pytest.raises(generation.LedgerUnreadable):
        generation.load_attempts("s002")


def test_every_record_atomic_writes_is_read_through_atomic():
    """The enumeration, made mechanical.

    Adding a fourth ``atomic.write_json`` destination without a matching
    ``atomic.read_json`` on its read path is how this defect comes back. Grepping
    the source is crude, and it is exactly as crude as the mistake it catches.
    """
    root = Path(__file__).resolve().parent.parent / "backend"
    writers = sorted(p.name for p in root.glob("*.py")
                     if "atomic.write_json" in p.read_text(encoding="utf-8"))
    assert writers == ["director.py", "exports.py", "generation.py", "main.py",
                       "slots.py"], (
        f"the set of modules writing atomic records changed to {writers}. Each new "
        f"one needs its READ path routed through atomic.read_json, and a case above "
        f"proving it survives a denial.")
    readers = sorted(p.name for p in root.glob("*.py")
                     if "atomic.read_json" in p.read_text(encoding="utf-8"))
    assert readers == ["director.py", "exports.py", "generation.py", "slots.py"], (
        f"modules reading atomic records changed to {readers}")


def test_no_reader_of_an_atomic_record_opens_it_directly():
    """The bare form is the defect. It must not come back into these three."""
    root = Path(__file__).resolve().parent.parent / "backend"
    for name, path_fn in (("director.py", "plan_path"),
                          ("exports.py", "snapshot_path"),
                          ("generation.py", "ledger_path"),
                          ("slots.py", "slots_path")):
        src = (root / name).read_text(encoding="utf-8")
        assert "read_text(encoding=" not in src, (
            f"{name} reads a record with a bare read_text again; route it through "
            f"atomic.read_json ({path_fn} is written by atomic.write_json)")


# --- and the shape of a record survives the round trip ---------------------------

def test_read_json_returns_what_write_json_wrote(tmp_path):
    p = tmp_path / "rec.json"
    payload = {"beat_id": "s001", "attempts": [{"id": "a", "cost": 0.15}], "n": None}
    atomic.write_json(p, payload)
    assert atomic.read_json(p) == payload
    assert json.loads(p.read_text(encoding="utf-8")) == payload
