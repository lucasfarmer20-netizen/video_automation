"""Every Storyboard field must survive to_dict -> from_dict.

`to_dict` uses `asdict`, so it serialises whatever the dataclass has. `from_dict`
rebuilds with an EXPLICIT field list, so a field added to the dataclass is written
to disk and silently dropped on every read — the value is persisted, and reading
it back returns the default.

That is what happened to `vo_profile`: assigning a narrator profile returned
{"ok": true, "vo_profile": "historical_docudrama"}, the manifest on disk held it,
and the next read said "". The endpoint reported success and the setting never
took effect.

This test fails the moment a field is added to Storyboard without updating
from_dict, which is the only reliable defence against a hand-maintained
constructor.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.manifest import Camera, Shot, Storyboard  # noqa: E402

# Structural fields handled separately by from_dict (nested dataclasses, the id,
# and shots). Everything else must round-trip by value.
STRUCTURAL = {"id", "shots", "render", "motion", "grade", "mix"}

# A non-default value per scalar field, so a dropped field shows up as a
# difference rather than coincidentally matching its default.
PROBES: dict[str, object] = {
    "version": 99,
    "title": "Round Trip Test",
    "channel": "calluses",
    "cultural_origin": "Klondike",
    "script_locked": True,
    "storyboard_approved": True,
    "music_track": "probe_track.mp3",
    "music_prompt": "sparse, no percussion",
    "voice_id": "probeVoiceId",
    "vo_profile": "historical_docudrama",
}


def _roundtrip(sb: Storyboard) -> Storyboard:
    d = sb.to_dict()
    return Storyboard.from_dict(sb.id or "probe", d, d.get("shots") or [])


def test_every_scalar_field_has_a_probe():
    """If Storyboard gains a field, this test must be taught about it."""
    scalars = {f for f in Storyboard.__dataclass_fields__ if f not in STRUCTURAL}
    missing = scalars - set(PROBES)
    assert not missing, (
        f"Storyboard gained field(s) {sorted(missing)}. Add a probe value here, "
        f"and make sure from_dict actually reads them — a field it does not "
        f"mention is saved and then silently lost on the next load."
    )


@pytest.mark.parametrize("field", sorted(PROBES))
def test_field_survives_roundtrip(field):
    sb = Storyboard(id="probe", title="x", shots=[Shot(scene_id="s001", camera=Camera())])
    setattr(sb, field, PROBES[field])
    back = _roundtrip(sb)
    assert getattr(back, field) == PROBES[field], (
        f"{field!r} did not survive to_dict -> from_dict: "
        f"wrote {PROBES[field]!r}, read {getattr(back, field)!r}. "
        f"from_dict rebuilds with an explicit field list and is missing this one."
    )


def test_shots_and_nested_config_survive():
    sb = Storyboard(id="probe", title="x",
                    shots=[Shot(scene_id="s001", narration="hello",
                                camera=Camera(duration=12.5, move="pan_left"))])
    sb.render.variations = 4
    sb.mix.sfx = 0.42
    back = _roundtrip(sb)
    assert len(back.shots) == 1
    assert back.shots[0].narration == "hello"
    assert back.shots[0].camera.duration == 12.5
    assert back.shots[0].camera.move == "pan_left"
    assert back.render.variations == 4
    assert back.mix.sfx == 0.42


# --- Firestore beat reconciliation (review round 1, finding #5b) ---------------

class _FakeDoc:
    def __init__(self, doc_id): self.id = doc_id


class _FakeCollection:
    def __init__(self, store): self.store = store
    def document(self, doc_id): return _FakeDoc(doc_id)
    def list_documents(self): return [_FakeDoc(k) for k in list(self.store)]


class _FakeBatch:
    def __init__(self, store): self.store, self.deleted = store, []
    def set(self, ref, data): self.store[ref.id] = data
    def delete(self, ref): self.deleted.append(ref.id); self.store.pop(ref.id, None)
    def commit(self): pass


def test_shrinking_an_episode_deletes_the_beats_it_dropped(monkeypatch):
    """save_project upserted and never deleted, and load_project streams the whole
    subcollection -- so redrafting 25 beats down to 15 left 10 zombies carrying the
    old script's narration and approved=true, and the next read returned 25."""
    from backend import manifest as M

    beats_store = {f"s{i:03d}": {"scene_id": f"s{i:03d}", "narration": "old"}
                   for i in range(1, 26)}
    batches = []

    class _ProjRef:
        def set(self, data): pass
        def collection(self, name): return _FakeCollection(beats_store)

    class _DB:
        def collection(self, name):
            return type("C", (), {"document": lambda _s, _i: _ProjRef()})()
        def batch(self):
            b = _FakeBatch(beats_store); batches.append(b); return b

    monkeypatch.setattr(M, "db", _DB())

    sb = M.Storyboard(title="T", id="proj")
    sb.shots = [M.Shot(scene_id=f"s{i:03d}", narration="new", prompt="p",
                       camera=M.Camera(move="static", duration=6.0))
                for i in range(1, 16)]
    M.save_project(sb)

    assert sorted(batches[0].deleted) == [f"s{i:03d}" for i in range(16, 26)]
    assert len(beats_store) == 15
    assert all(v["narration"] == "new" for v in beats_store.values())
