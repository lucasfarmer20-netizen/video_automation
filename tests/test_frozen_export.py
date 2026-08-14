"""Frozen export (contract §9.1, §9.2, §11.7).

§11.7 is the clause these tests exist for:

    Final master and FCPXML must reflect the same frozen timeline state.

A test that checks both files exist proves nothing about that — two artifacts
built from two different readings of live state also both exist. So the shape of
the equivalence tests here is always the same: **live state is mutated in between
the two deliverables**, and both must still describe the cut as it stood at the
freeze. That is the only assertion that can tell a structurally frozen export
apart from one that merely happened not to change while it ran.

The master renderer is faked, and deliberately not the FCPXML writer. The real
``timeline.build_preview`` needs ffmpeg, librosa and scipy; the stand-in records
the beats it was handed, which is exactly the thing under test — *what state the
master was rendered from*, not how it was encoded. ``timeline.build`` is the real
one, so the FCPXML asserted against is a genuine one, parsed from its own bytes.
"""

from __future__ import annotations

import json
import sys
import types
import xml.etree.ElementTree as ET
from dataclasses import asdict
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
for _m in ("anthropic", "fal_client", "elevenlabs"):
    sys.modules.setdefault(_m, types.ModuleType(_m))

from backend import bundle, config, exports, slots, timeline  # noqa: E402
from backend.manifest import Camera, Shot, Storyboard  # noqa: E402
from backend.manifest import save as save_manifest  # noqa: E402


# --- fixtures -------------------------------------------------------------------

@pytest.fixture(autouse=True)
def project(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "MANIFEST_PATH", tmp_path / "storyboard_manifest.json")
    return tmp_path


def _sb(*beats, title="Frozen Cut") -> Storyboard:
    return Storyboard(title=title, storyboard_approved=True, shots=[
        Shot(scene_id=b, narration=f"line for {b}", approved=True,
             camera=Camera(move="static", duration=d))
        for b, d in beats])


def _renders(tmp_path: Path, *scene_ids) -> None:
    """Put a file where each beat's clip goes.

    ``timeline.build`` only stats these — it never probes a V1 clip — so a
    one-byte file is enough to make the beat a named clip in the FCPXML instead
    of an anonymous gap, which is what makes the XML assertable at all.
    """
    rd = tmp_path / "render"
    rd.mkdir(parents=True, exist_ok=True)
    for sid in scene_ids:
        (rd / f"{sid}.mp4").write_bytes(b"\0")


def _fake_master(monkeypatch, mutate=None):
    """A master renderer that records the state it was given, then optionally
    mutates live state.

    ``mutate`` is the whole point: it fires *between* the master and the FCPXML,
    so an export that re-read live state for the second deliverable produces two
    artifacts describing different cuts, and the equivalence assertions fail.
    """
    calls: list[dict] = []

    def fake(storyboard=None, render_dir=None, out=None, height=480):
        out = Path(out)
        out.parent.mkdir(parents=True, exist_ok=True)
        described = {
            "height": height,
            "beats": [{"scene_id": s.scene_id,
                       "duration": float(s.camera.duration)}
                      for s in storyboard.shots],
        }
        described["runtime"] = sum(b["duration"] for b in described["beats"])
        out.write_text(json.dumps(described, indent=2), encoding="utf-8")
        calls.append(described)
        if mutate is not None:
            mutate()
        return out, described["runtime"]

    monkeypatch.setattr(timeline, "build_preview", fake)
    return calls


def _spine(fcpxml: Path) -> list[tuple[str, str, str]]:
    """(name, offset, duration) for every clip in the FCPXML's spine.

    Parsed out of the file's own bytes rather than read back through an OTIO
    adapter, so what is asserted is the deliverable a human opens in Resolve.
    """
    root = ET.parse(fcpxml).getroot()
    spine = root.find(".//spine")
    assert spine is not None, "the FCPXML has no spine"
    return [(c.get("name"), c.get("offset"), c.get("duration")) for c in spine]


def _sequence_duration(fcpxml: Path) -> str:
    return ET.parse(fcpxml).getroot().find(".//sequence").get("duration")


def _master(tmp_path: Path, version: str) -> dict:
    return json.loads((exports.version_dir(version) / exports.MASTER_NAME)
                      .read_text(encoding="utf-8"))


def _fcpxml(version: str) -> Path:
    return exports.version_dir(version) / f"{exports.STEM}.fcpxml"


# --- §11.7 the equivalence, tested rather than asserted ---------------------------

def test_master_and_fcpxml_describe_the_frozen_cut_when_live_state_changes_between_them(
        tmp_path, monkeypatch):
    """The §11.7 test that matters.

    A beat is retimed 4s -> 20s and a third beat appears, in the window between
    the master render and the FCPXML write. Both deliverables must still describe
    the two-beat 6+4 cut that was frozen. An export that regenerated the FCPXML
    from live state would show 6+20 and a third clip.
    """
    sb = _sb(("s001", 6.0), ("s002", 4.0))
    _renders(tmp_path, "s001", "s002")

    def mutate():
        sb.shots[1].camera.duration = 20.0
        sb.shots.append(Shot(scene_id="s003", narration="late arrival",
                             camera=Camera(move="static", duration=9.0)))
        save_manifest(sb)

    _fake_master(monkeypatch, mutate=mutate)
    result = exports.run(sb, preset="h264_1080p", log=lambda m: None)

    # Live state really did change — otherwise this test proves nothing.
    assert [s.scene_id for s in sb.shots] == ["s001", "s002", "s003"]
    assert sb.shots[1].camera.duration == 20.0

    master = _master(tmp_path, result["version"])
    assert master["beats"] == [{"scene_id": "s001", "duration": 6.0},
                               {"scene_id": "s002", "duration": 4.0}]

    spine = _spine(_fcpxml(result["version"]))
    assert spine == [("s001", "0s", "6/1s"), ("s002", "6s", "4/1s")]
    assert _sequence_duration(_fcpxml(result["version"])) == "10/1s"


def test_a_manifest_rewritten_mid_export_cannot_reach_either_deliverable(
        tmp_path, monkeypatch):
    """The same window, closed from the other side.

    Here the mutation is to the manifest FILE, not to the object handed in. It
    catches the variant where the second deliverable is produced from a fresh
    ``manifest.load()`` rather than from the argument — which is what the old
    ``/api/export/{kind}`` route effectively did, and it looks correct at the call
    site because it re-reads "the truth".
    """
    sb = _sb(("s001", 6.0), ("s002", 4.0))
    _renders(tmp_path, "s001", "s002")

    def mutate():
        edited = _sb(("s001", 30.0), ("s002", 30.0))
        save_manifest(edited)

    _fake_master(monkeypatch, mutate=mutate)
    result = exports.run(sb, log=lambda m: None)

    from backend import manifest as manifest_mod
    assert [float(s.camera.duration)
            for s in manifest_mod.load().shots] == [30.0, 30.0]

    assert _spine(_fcpxml(result["version"])) == [("s001", "0s", "6/1s"),
                                                  ("s002", "6s", "4/1s")]
    assert _master(tmp_path, result["version"])["runtime"] == 10.0


def test_master_fcpxml_and_snapshot_all_describe_one_state(tmp_path, monkeypatch):
    """Three-way agreement, not two-way.

    Master and FCPXML agreeing with each other is necessary and not sufficient:
    they could agree on a cut the snapshot does not describe, which would leave
    the deliverables unverifiable against the thing that is supposed to identify
    them. So the snapshot's own frozen state is the third leg.
    """
    sb = _sb(("s001", 6.0), ("s002", 4.0), ("s003", 2.5))
    _renders(tmp_path, "s001", "s002", "s003")
    _fake_master(monkeypatch)
    result = exports.run(sb, log=lambda m: None)
    version = result["version"]

    snapshot = exports.load_snapshot(version)
    frozen_beats = [(s["scene_id"], s["camera"]["duration"])
                    for s in snapshot["frozen"]["storyboard"]["shots"]]

    master_beats = [(b["scene_id"], b["duration"])
                    for b in _master(tmp_path, version)["beats"]]
    xml_beats = [(name, dur) for name, _off, dur in _spine(_fcpxml(version))]

    assert master_beats == frozen_beats
    assert xml_beats == [("s001", "6/1s"), ("s002", "4/1s"), ("s003", "5/2s")]
    assert result["snapshot_id"] == snapshot["snapshot_id"]


def test_the_frozen_storyboard_is_reconstructed_exactly_once(tmp_path, monkeypatch):
    """The seam that makes equivalence structural.

    Two ``restore()`` calls would be two objects, and two objects is how the two
    deliverables come to disagree again. So this counts the calls: one per export,
    and both deliverables get the same object.
    """
    seen: list[int] = []
    real_restore = exports.restore

    def counting(snapshot):
        sb = real_restore(snapshot)
        seen.append(id(sb))
        return sb

    monkeypatch.setattr(exports, "restore", counting)
    handed: list[int] = []
    real_build = timeline.build

    def spy_build(storyboard=None, **kw):
        handed.append(id(storyboard))
        return real_build(storyboard, **kw)

    monkeypatch.setattr(timeline, "build", spy_build)

    sb = _sb(("s001", 6.0))
    _renders(tmp_path, "s001")

    def record_master(storyboard=None, render_dir=None, out=None, height=480):
        handed.append(id(storyboard))
        Path(out).parent.mkdir(parents=True, exist_ok=True)
        Path(out).write_text("{}", encoding="utf-8")
        return Path(out), 6.0

    monkeypatch.setattr(timeline, "build_preview", record_master)
    exports.run(sb, log=lambda m: None)

    assert len(seen) == 1, "the snapshot must be restored once, not per deliverable"
    assert handed == [seen[0], seen[0]], \
        "both deliverables must be produced from the same restored object"
    assert id(sb) not in handed, "no deliverable may be produced from live state"


# --- §9.1 the snapshot ------------------------------------------------------------

def test_snapshot_identifies_every_piece_of_state_9_1_enumerates(tmp_path, monkeypatch):
    _fake_master(monkeypatch)
    sb = _sb(("s001", 6.0))
    _renders(tmp_path, "s001")
    version = exports.run(sb, log=lambda m: None)["version"]

    state = exports.load_snapshot(version)["state"]
    assert set(state) == set(exports.STATE_KEYS)
    assert set(exports.STATE_KEYS) == {
        "project_version", "script_timing", "director_plan", "approved_shots",
        "selected_outputs", "timeline", "audio", "grade"}
    assert all(str(v).startswith("sha256:") for v in state.values())


def test_snapshot_digests_recompute_from_the_state_the_export_came_from(
        tmp_path, monkeypatch):
    _fake_master(monkeypatch)
    sb = _sb(("s001", 6.0), ("s002", 4.0))
    _renders(tmp_path, "s001", "s002")
    version = exports.run(sb, log=lambda m: None)["version"]

    result = exports.verification(version)
    assert result["verifiable"] and result["ok"], result
    assert result["mismatched"] == []


def test_a_snapshot_edited_after_the_fact_fails_verification(tmp_path, monkeypatch):
    """The digests have to be load-bearing.

    A snapshot that only *claims* to identify eight things is worth nothing if
    nobody can tell when the claim stopped being true. Retiming a beat inside the
    stored state must break both the state digest and the id.
    """
    _fake_master(monkeypatch)
    sb = _sb(("s001", 6.0))
    _renders(tmp_path, "s001")
    version = exports.run(sb, log=lambda m: None)["version"]

    p = exports.snapshot_path(version)
    doc = json.loads(p.read_text(encoding="utf-8"))
    doc["frozen"]["storyboard"]["shots"][0]["camera"]["duration"] = 99.0
    p.write_text(json.dumps(doc), encoding="utf-8")

    result = exports.verification(version)
    assert result["verifiable"] and not result["ok"]
    assert "script_timing" in result["mismatched"]
    assert "snapshot_id" in result["mismatched"]


def test_each_state_digest_changes_when_its_own_state_changes(tmp_path, monkeypatch):
    """The test that makes the eight digests load-bearing instead of decorative.

    ``verify()`` proves a snapshot is internally consistent — that its digests
    recompute from the state stored beside them. On its own that is not enough,
    and the gap is a quiet one: a digest computed over an empty dict is perfectly
    self-consistent and identifies nothing. Verification would still pass, the
    §9.1 key would still be present, and the state it was supposed to cover could
    change freely without the snapshot noticing.

    So each of the eight is exercised against a change to the state it claims to
    cover, and must move. This is what stops "the snapshot identifies the grade
    state" from being a sentence in a docstring.

    **Several cases per §9.1 item, not one.** A single change per key leaves a
    digest covered only for the one field that change touches, and the review of
    this slice found exactly that: three of the reviewer's own mutations survived
    (aggregate `storyboard_approved` dropped, `sfx_layers` dropped from audio, the
    timeline digest reduced to slot ids) because the one case for each key
    happened to move some *other* field in the same digest.

    The timeline case was the sharpest lesson and the reason ``prepare`` exists.
    It used to go from *no slots file* to *one slot*, so the digest moved because
    the cut came into existence — which even an ids-only digest notices. A case
    that proves the digest covers a slot's *contents* has to start from a saved
    cut and change a field inside it, leaving the slot set identical.
    """
    from backend import director
    from backend.manifest import AudioLayer

    _fake_master(monkeypatch)
    _renders(tmp_path, "s001", "s002")

    def _plan(sb):
        director.save_plan(director.CoveragePlan(
            beat_id="s001", beat_duration=6.0, status="draft",
            coverage=[director.DirectorShot(id="s001.01", beat_id="s001",
                                            camera=Camera(duration=6.0))]))

    def _save_cut(sb):
        cut = slots.build(sb)
        cut[0].media = "render/s001.mp4"
        slots.save(cut)

    def _trim_slot(sb):
        cut = slots.load()
        cut[0].trim_in = 1.25
        slots.save(cut)

    def _swap_take(sb):
        cut = slots.load()
        cut[0].media = "render/s001_take2.mp4"
        cut[0].source_attempt = "att-2"
        slots.save(cut)

    def _add_layer(sb):
        sb.shots[0].sfx_layers = [AudioLayer(id="L1", label="wind", gain=0.6)]

    def _tune_layer(sb):
        sb.shots[0].sfx_layers[0].gain = 0.95
        sb.shots[0].sfx_layers[0].offset = -0.4

    # (§9.1 key, what this case proves the digest covers, prepare, change).
    # ``prepare`` runs before the first freeze, so the change is the ONLY
    # difference between the two snapshots.
    cases = [
        ("project_version", "the culture the episode is illustrated in",
         None, lambda sb: setattr(sb, "cultural_origin", "Slavic")),
        ("script_timing", "the narration text",
         None, lambda sb: setattr(sb.shots[0], "narration", "rewritten")),
        ("script_timing", "a beat's duration",
         None, lambda sb: setattr(sb.shots[0].camera, "duration", 9.5)),
        ("director_plan", "the coverage plan for a beat",
         None, _plan),
        ("approved_shots", "one beat's approval",
         None, lambda sb: setattr(sb.shots[0], "approved", False)),
        ("approved_shots", "the AGGREGATE storyboard approval",
         None, lambda sb: setattr(sb, "storyboard_approved", False)),
        ("selected_outputs", "which take a beat uses",
         None, lambda sb: setattr(sb.shots[0], "chosen_variation", 3)),
        ("timeline", "a slot's trim, with the slot set unchanged",
         _save_cut, _trim_slot),
        ("timeline", "the media in a slot, with the slot set unchanged",
         _save_cut, _swap_take),
        ("audio", "the episode mix bus",
         None, lambda sb: setattr(sb.mix, "narration", 0.42)),
        ("audio", "a layer of ambience appearing on a beat",
         None, _add_layer),
        ("audio", "an existing layer's gain and offset",
         _add_layer, _tune_layer),
        ("grade", "a per-beat grade override",
         None, lambda sb: setattr(sb.shots[0], "grade", {"contrast": 0.4})),
    ]
    assert {key for key, *_ in cases} == set(exports.STATE_KEYS), \
        "every §9.1 item needs at least one change that must move its digest"

    for n, (key, covers, prepare, change) in enumerate(cases, start=1):
        # Each case starts from nothing on disk. The stores outlive a case
        # otherwise, and a leftover plan or cut makes the next case's "before"
        # something other than what it reads as.
        slots.slots_path().unlink(missing_ok=True)
        for stale in director.director_dir().glob("*.json"):
            stale.unlink()

        sb = _sb(("s001", 6.0), ("s002", 4.0))
        if prepare is not None:
            prepare(sb)
        before = exports.freeze(sb, version=f"a{n}")["state"]
        change(sb)
        after = exports.freeze(sb, version=f"b{n}")["state"]
        assert after[key] != before[key], (
            f"the {key} digest did not move when {covers} changed — it does not "
            f"identify what §9.1 says it identifies")


def test_the_director_plan_digest_covers_the_plan_schema_version(
        tmp_path, monkeypatch):
    """§9.1 names "Director plan version", so the schema version is part of it.

    Split out from the case above because the change is to a module constant
    rather than to project state: a plan schema bump has to move the digest, or a
    snapshot could be verified against plans whose meaning changed underneath it.
    """
    from backend import director

    _fake_master(monkeypatch)
    sb = _sb(("s001", 6.0))
    before = exports.freeze(sb, version="v1")["state"]["director_plan"]
    monkeypatch.setattr(director, "PLAN_VERSION", director.PLAN_VERSION + 1)
    after = exports.freeze(sb, version="v2")["state"]["director_plan"]
    assert after != before


def test_the_snapshot_records_the_director_plan_and_its_approval(tmp_path, monkeypatch):
    from backend import director

    _fake_master(monkeypatch)
    sb = _sb(("s001", 6.0))
    _renders(tmp_path, "s001")
    plan = director.CoveragePlan(beat_id="s001", beat_duration=6.0, status="locked",
                                 coverage=[director.DirectorShot(
                                     id="s001.01", beat_id="s001",
                                     camera=Camera(duration=6.0),
                                     motion_type="parallax")])
    director.approve(plan, by="test", beat=sb.shots[0])
    director.save_plan(plan)

    version = exports.run(sb, log=lambda m: None)["version"]
    frozen = exports.load_snapshot(version)["frozen"]

    assert frozen["director_plan_version"] == director.PLAN_VERSION
    recorded = frozen["director_plans"]["s001"]
    assert recorded["status"] == "locked"
    assert recorded["approved_signature"] == plan.approved_signature
    assert recorded["plan_signature"] == director.plan_signature(plan)
    assert [c["id"] for c in recorded["coverage"]] == ["s001.01"]


def test_the_snapshot_records_the_slot_cut_and_the_selected_outputs(
        tmp_path, monkeypatch):
    _fake_master(monkeypatch)
    sb = _sb(("s001", 6.0))
    sb.shots[0].chosen_variation = 2
    sb.shots[0].draft_image = "assets/s001/var_2.png"
    _renders(tmp_path, "s001")
    cut = slots.build(sb)
    cut[0].media = "render/s001.mp4"
    cut[0].trim_in = 0.5
    slots.save(cut)

    version = exports.run(sb, log=lambda m: None)["version"]
    frozen = exports.load_snapshot(version)["frozen"]

    assert [s["id"] for s in frozen["timeline_slots"]] == [cut[0].id]
    assert frozen["timeline_slots"][0]["trim_in"] == 0.5
    assert frozen["timeline_slots"][0]["media"] == "render/s001.mp4"
    assert frozen["storyboard"]["shots"][0]["chosen_variation"] == 2


def test_an_unreadable_cut_stops_the_export_instead_of_freezing_an_empty_one(
        tmp_path, monkeypatch):
    """A cut that cannot be read is not an empty cut.

    ``slots`` already makes that argument for its own callers. It is stronger
    here: an export that froze the cut as empty because the file was busy would
    write a snapshot asserting a timeline nobody approved, and then bind a master
    to it.
    """
    _fake_master(monkeypatch)
    sb = _sb(("s001", 6.0))
    _renders(tmp_path, "s001")
    slots.slots_path().parent.mkdir(parents=True, exist_ok=True)
    slots.slots_path().write_text("{not json", encoding="utf-8")

    with pytest.raises(ValueError):
        exports.run(sb, version="v1", log=lambda m: None)

    assert not exports.version_dir("v1").exists(), \
        "a refused freeze must leave no version directory behind"
    assert exports.history() == []


def test_restore_rebuilds_the_frozen_storyboard_and_ignores_live_state(
        tmp_path, monkeypatch):
    _fake_master(monkeypatch)
    sb = _sb(("s001", 6.0), ("s002", 4.0))
    _renders(tmp_path, "s001", "s002")
    version = exports.run(sb, log=lambda m: None)["version"]

    sb.shots[0].camera.duration = 99.0
    save_manifest(sb)

    rebuilt = exports.restore(exports.load_snapshot(version))
    assert [float(s.camera.duration) for s in rebuilt.shots] == [6.0, 4.0]
    assert [s.scene_id for s in rebuilt.shots] == ["s001", "s002"]


def test_restoring_a_snapshot_loses_nothing_the_snapshot_identifies(
        tmp_path, monkeypatch):
    """The gap between "the snapshot identifies X" and "the export came from X".

    Both deliverables are produced from ``restore(snapshot)``, so the snapshot
    only really binds them if restoring it is lossless over the state its digests
    cover. ``Storyboard.from_dict`` deliberately drops fields it does not
    recognise — correctly, it must never take a project offline over a stray key —
    and that same tolerance means a field the digests cover but ``from_dict``
    silently defaults would leave the master describing something the snapshot
    does not.

    ``verify()`` cannot see this: it recomputes digests from the stored dict, not
    from the restored object, so it would pass either way. Re-freezing the
    RESTORED storyboard and comparing the storyboard-derived digests is the check
    that can.
    """
    _fake_master(monkeypatch)
    sb = _sb(("s001", 6.0), ("s002", 4.0))
    sb.cultural_origin = "Slavic"
    sb.music_track = "bed.wav"
    sb.mix.narration = 0.9
    sb.mix.solo = "sfx"
    sb.grade.contrast = 0.3
    sb.voice_id = "vsp"
    sb.shots[0].grade = {"brightness": 0.2}
    sb.shots[0].chosen_variation = 1
    sb.shots[0].draft_image = "assets/s001/var_1.png"
    sb.shots[0].offset_narration = 0.75
    sb.shots[0].gain_sfx = 1.4
    sb.shots[1].approved = False
    sb.shots[1].sfx_layers = [__import__("backend.manifest", fromlist=["AudioLayer"])
                              .AudioLayer(id="L1", label="wind", gain=0.6, offset=-0.4)]

    original = exports.freeze(sb, version="v1")
    rebuilt = exports.restore(original)
    again = exports.freeze(rebuilt, version="v2")

    from_storyboard = ("project_version", "script_timing", "approved_shots",
                       "selected_outputs", "audio", "grade")
    for key in from_storyboard:
        assert again["state"][key] == original["state"][key], (
            f"restoring the snapshot changed its {key} state — the deliverables "
            f"are produced from the restored object, so they would not describe "
            f"what the snapshot identifies")
    assert again["snapshot_id"] == original["snapshot_id"]


def test_a_version_name_cannot_escape_the_exports_directory():
    for bad in ("../../etc", "..", "a/b", "/abs", ".hidden", "", "v1/../v2"):
        with pytest.raises(exports.ExportError):
            exports.version_dir(bad)


# --- §9.1 a prior master is never overwritten -------------------------------------

def test_freeze_refuses_a_version_that_already_exists(tmp_path, monkeypatch):
    _fake_master(monkeypatch)
    sb = _sb(("s001", 6.0))
    _renders(tmp_path, "s001")
    exports.freeze(sb, version="v1")
    with pytest.raises(exports.ExportError, match="already exists"):
        exports.freeze(sb, version="v1")


def test_a_later_edit_creates_a_new_version_and_leaves_the_prior_master_intact(
        tmp_path, monkeypatch):
    """§9.1's second sentence. The old deliverables must be byte-identical after."""
    _fake_master(monkeypatch)
    sb = _sb(("s001", 6.0), ("s002", 4.0))
    _renders(tmp_path, "s001", "s002")
    first = exports.run(sb, preset="h264", log=lambda m: None)

    v1_master = (exports.version_dir(first["version"]) / exports.MASTER_NAME).read_bytes()
    v1_xml = _fcpxml(first["version"]).read_bytes()
    v1_snapshot = exports.snapshot_path(first["version"]).read_bytes()

    sb.shots[1].camera.duration = 12.0
    save_manifest(sb)
    second = exports.run(sb, preset="h264", log=lambda m: None)

    assert second["version"] != first["version"]
    assert second["snapshot_id"] != first["snapshot_id"]

    assert (exports.version_dir(first["version"]) / exports.MASTER_NAME).read_bytes() == v1_master
    assert _fcpxml(first["version"]).read_bytes() == v1_xml
    assert exports.snapshot_path(first["version"]).read_bytes() == v1_snapshot

    assert _spine(_fcpxml(first["version"])) == [("s001", "0s", "6/1s"),
                                                 ("s002", "6s", "4/1s")]
    assert _spine(_fcpxml(second["version"])) == [("s001", "0s", "6/1s"),
                                                 ("s002", "6s", "12/1s")]


def test_next_version_skips_a_directory_history_never_recorded(tmp_path):
    """A crash between mkdir and the history append must not cost a master."""
    (exports.exports_dir() / "v7").mkdir(parents=True)
    assert exports.history() == []
    assert exports.next_version() == "v8"


def test_next_version_skips_a_version_history_recorded_without_a_directory():
    exports.record("v4", "master", "succeeded")
    assert exports.next_version() == "v5"


# --- §9.2 export history ----------------------------------------------------------

def test_history_retains_the_six_fields_9_2_names(tmp_path, monkeypatch):
    _fake_master(monkeypatch)
    sb = _sb(("s001", 6.0))
    _renders(tmp_path, "s001")
    result = exports.run(sb, preset="prores_422", log=lambda m: None)

    rows = exports.history()
    assert rows, "an export must appear in history"
    for row in rows:
        for field in ("version", "type", "preset", "timestamp", "status",
                      "snapshot_id"):
            assert field in row, f"history row is missing {field}: {row}"
    master_rows = [r for r in rows if r["type"] == "master"]
    assert len(master_rows) == 1
    assert master_rows[0]["version"] == result["version"]
    assert master_rows[0]["preset"] == "prores_422"
    assert master_rows[0]["status"] == "succeeded"
    assert master_rows[0]["snapshot_id"] == result["snapshot_id"]
    assert {r["type"] for r in rows} >= {"snapshot", "master", "fcpxml"}


def test_history_is_append_only(tmp_path, monkeypatch):
    """Rows accumulate; none is ever rewritten.

    A status recorded by mutating the earlier row would make history a record of
    the present rather than of what happened — and then a crash between the two
    writes leaves no trace at all instead of a ``started`` with no terminal row.
    """
    _fake_master(monkeypatch)
    sb = _sb(("s001", 6.0))
    _renders(tmp_path, "s001")

    exports.run(sb, log=lambda m: None)
    after_first = exports.history()
    assert [r["status"] for r in after_first][0] == "started"
    assert "started" in {r["status"] for r in after_first}
    assert "succeeded" in {r["status"] for r in after_first}

    exports.run(sb, log=lambda m: None)
    after_second = exports.history()

    assert len(after_second) > len(after_first)
    assert after_second[:len(after_first)] == after_first, \
        "an earlier history row was rewritten"


def test_a_failed_deliverable_is_recorded_as_failed_not_omitted(
        tmp_path, monkeypatch):
    """An export missing a §9.2 deliverable must not read as complete."""
    _fake_master(monkeypatch)

    def boom(*a, **kw):
        raise RuntimeError("adapter exploded")

    monkeypatch.setattr(timeline, "build", boom)
    sb = _sb(("s001", 6.0))
    _renders(tmp_path, "s001")

    with pytest.raises(RuntimeError, match="adapter exploded"):
        exports.run(sb, version="v1", log=lambda m: None)

    rows = exports.history()
    fcpxml_rows = [r for r in rows if r["type"] == "fcpxml"]
    assert len(fcpxml_rows) == 1
    assert fcpxml_rows[0]["status"] == "failed"
    assert "adapter exploded" in fcpxml_rows[0]["note"]
    assert [r["status"] for r in rows if r["type"] == "master"] == ["succeeded"]


def test_an_fcpxml_the_adapter_could_not_write_is_recorded_failed(
        tmp_path, monkeypatch):
    """``timeline.build`` returns None for the FCPXML when the adapter fails.

    That is a soft failure by design — the .otio is still valid — but §9.2 names
    FCPXML as required, so the export must not report it as delivered.
    """
    _fake_master(monkeypatch)
    real_build = timeline.build

    def no_xml(storyboard=None, **kw):
        otio_path, _fcpxml, runtime = real_build(storyboard, **kw)
        return otio_path, None, runtime

    monkeypatch.setattr(timeline, "build", no_xml)
    sb = _sb(("s001", 6.0))
    _renders(tmp_path, "s001")
    result = exports.run(sb, version="v1", log=lambda m: None)

    assert "fcpxml" not in result["artifacts"]
    fcpxml_rows = [r for r in exports.history() if r["type"] == "fcpxml"]
    assert [r["status"] for r in fcpxml_rows] == ["failed"]


def test_a_history_write_that_fails_does_not_replace_the_export_error(
        tmp_path, monkeypatch):
    """The failure path of the failure path.

    ``_record_failure`` writes to disk, so it can fail too. Raising from there
    would surface a history-write error in place of the export error that actually
    happened, and whoever reads the job log debugs the wrong thing entirely.
    """
    _fake_master(monkeypatch)

    def boom(*a, **kw):
        raise RuntimeError("adapter exploded")

    monkeypatch.setattr(timeline, "build", boom)

    real_record = exports.record

    def refuse_failures(version, kind, status, **kw):
        if status == "failed":
            raise OSError("the exports volume is read-only")
        return real_record(version, kind, status, **kw)

    monkeypatch.setattr(exports, "record", refuse_failures)

    sb = _sb(("s001", 6.0))
    _renders(tmp_path, "s001")
    with pytest.raises(RuntimeError, match="adapter exploded"):
        exports.run(sb, version="v1", log=lambda m: None)


# --- migration: legacy exports are never given a snapshot -------------------------

def test_a_pre_snapshot_export_is_recorded_legacy_with_no_snapshot_id(tmp_path):
    sb = _sb(("s001", 6.0))
    slug = config.episode_paths(sb.title)["slug"]
    (tmp_path / f"{slug}.fcpxml").write_text("<fcpxml/>", encoding="utf-8")
    (tmp_path / f"{slug}.otio").write_text("{}", encoding="utf-8")

    added = exports.record_legacy(sb)
    assert {r["type"] for r in added} == {"fcpxml", "otio"}
    for row in added:
        assert row["status"] == "legacy"
        assert row["snapshot_id"] == ""
        assert "unverifiable" in row["note"]

    # And specifically not the id of whatever state happens to be live now. That
    # is the shape the fabrication would take, and it is the worst of the options:
    # it reads as provenance, it verifies, and it asserts that this artifact came
    # from this state — which nobody recorded and nobody can know.
    live = exports.snapshot_id(exports._frozen_state(sb))
    assert all(r["snapshot_id"] != live for r in exports.history())


def test_no_snapshot_is_fabricated_for_a_legacy_export(tmp_path):
    """The approved migration constraint, tested as an absence.

    Nothing may invent provenance for an artifact whose provenance nobody
    recorded — that would assert an equivalence no one is in a position to claim.
    So: no snapshot file appears anywhere, and verification says unverifiable
    rather than failing closed with a reason that sounds like a bug.
    """
    sb = _sb(("s001", 6.0))
    slug = config.episode_paths(sb.title)["slug"]
    (tmp_path / f"{slug}.fcpxml").write_text("<fcpxml/>", encoding="utf-8")
    exports.record_legacy(sb)

    assert list(exports.exports_dir().rglob(exports.SNAPSHOT_NAME)) == []

    row = exports.history()[0]
    result = exports.verification(row["version"])
    assert result["verifiable"] is False
    assert result["ok"] is False

    # Precisely: no provenance was ever recorded. NOT "the snapshot is
    # unreadable", which is a different claim — it says one exists and something
    # is wrong with it, and it invites someone to go looking for the file or to
    # regenerate it. An earlier version of this assertion checked only for
    # "legacy" in the reason, and passed under a mutation that removed the legacy
    # arm entirely: the fallback error quotes the version name, which is
    # "legacy:<file>", so the substring was there for the wrong reason. The
    # mutation harness is what caught it.
    assert "no snapshot provenance recorded" in result["reason"]
    assert "unreadable" not in result["reason"], (
        "a legacy export must be reported as having no provenance, not as a "
        f"faulty snapshot: {result['reason']!r}")


def test_recording_legacy_exports_twice_neither_duplicates_nor_rewrites(tmp_path):
    sb = _sb(("s001", 6.0))
    slug = config.episode_paths(sb.title)["slug"]
    (tmp_path / f"{slug}.fcpxml").write_text("<fcpxml/>", encoding="utf-8")

    exports.record_legacy(sb)
    first = exports.history()
    assert exports.record_legacy(sb) == []
    assert exports.history() == first


def test_a_legacy_row_does_not_consume_a_version_number(tmp_path):
    """``legacy:<file>`` must not be mistaken for ``v<N>`` when numbering."""
    sb = _sb(("s001", 6.0))
    slug = config.episode_paths(sb.title)["slug"]
    (tmp_path / f"{slug}.fcpxml").write_text("<fcpxml/>", encoding="utf-8")
    exports.record_legacy(sb)
    assert exports.next_version() == "v1"


# --- the API surface --------------------------------------------------------------

@pytest.fixture
def studio(tmp_path, monkeypatch):
    """A client over the real app, with the export job run inline.

    ``start_job`` is replaced with a synchronous call rather than stubbed out, so
    these exercise the export the endpoint actually performs. Stubbing it would
    leave the routes tested and the work untested, which for this slice is the
    half that matters.
    """
    from fastapi.testclient import TestClient
    from backend import main as M

    monkeypatch.setattr(config, "MANIFEST_PATH", tmp_path / "storyboard_manifest.json")
    sb = _sb(("s001", 6.0), ("s002", 4.0))
    monkeypatch.setattr(M, "get_current_project", lambda: sb)
    monkeypatch.setattr(M, "start_job", lambda name, fn, *a, **k: (fn(), True)[1])
    return TestClient(M.app, raise_server_exceptions=False), sb


def test_export_history_is_not_shadowed_by_the_export_type_route(studio, tmp_path):
    """``/api/export/{kind}`` matches one segment and would answer "history".

    Route order in FastAPI is declaration order, so this is a real defect class
    and not a hypothetical: the 404 it produces looks like "no export yet".
    """
    client, _sb = studio
    res = client.get("/api/export/history")
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["ok"] is True
    assert "exports" in body and "verification" in body


def test_the_endpoint_freezes_and_serves_both_deliverables(studio, tmp_path,
                                                           monkeypatch):
    client, sb = studio
    _renders(tmp_path, "s001", "s002")
    _fake_master(monkeypatch)

    res = client.post("/api/export/master", json={"preset": "h264_1080p"})
    assert res.status_code == 200, res.text
    version = res.json()["version"]

    for kind in ("snapshot", "master", "fcpxml", "otio"):
        got = client.get(f"/api/export/version/{version}/{kind}")
        assert got.status_code == 200, f"{kind}: {got.text}"

    rows = client.get("/api/export/history").json()
    assert any(r["version"] == version and r["type"] == "master"
               and r["status"] == "succeeded" for r in rows["exports"])
    assert any(v["version"] == version and v["ok"] for v in rows["verification"])


def test_a_traversal_version_is_refused_by_the_download_route(studio):
    client, _sb = studio
    res = client.get("/api/export/version/..%2F..%2Fetc/snapshot")
    assert res.status_code in (400, 404), res.text
    assert res.status_code != 200


def test_an_unknown_artifact_kind_is_not_served(studio, tmp_path, monkeypatch):
    client, _sb = studio
    _renders(tmp_path, "s001", "s002")
    _fake_master(monkeypatch)
    version = client.post("/api/export/master", json={}).json()["version"]
    assert client.get(f"/api/export/version/{version}/manifest").status_code == 404


# --- the bundle carries its provenance ------------------------------------------

def test_a_bundle_built_from_a_frozen_version_names_its_snapshot(
        tmp_path, monkeypatch):
    import zipfile

    _fake_master(monkeypatch)
    sb = _sb(("s001", 6.0))
    _renders(tmp_path, "s001")
    result = exports.run(sb, log=lambda m: None)

    zip_path = bundle.build(sb, log=lambda m: None, version=result["version"])
    assert zip_path.parent == exports.version_dir(result["version"])
    with zipfile.ZipFile(zip_path) as z:
        readme = z.read("README.txt").decode("utf-8")
    assert f"frozen export {result['version']}" in readme
    assert result["snapshot_id"] in readme


def test_a_bundle_with_no_frozen_export_says_so(tmp_path, monkeypatch):
    import zipfile

    sb = _sb(("s001", 6.0))
    _renders(tmp_path, "s001")
    timeline.build(sb)

    zip_path = bundle.build(sb, log=lambda m: None)
    with zipfile.ZipFile(zip_path) as z:
        readme = z.read("README.txt").decode("utf-8")
    assert "cannot be\nverified" in readme or "cannot be verified" in readme
    assert "No frozen export snapshot" in readme
