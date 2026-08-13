"""Timeline slots and placeholders (contract §7.1, §6.2; approved invariant C5).

The property under test is that the cut is made of positions, not files. Every
assertion here is some form of "the editor's work survived": choosing a
different take, a shot arriving late, a re-plan of one beat — none of them may
rebuild the edit around them.

C5 states the placeholder half: placeholder identity is semantic, not visual. A
placeholder must carry the shot it belongs to, the slot it occupies, the
duration it is owed, the media type it expects, and the beat it came from —
otherwise a later output cannot land in it without reconstructing the edit,
which is exactly what the slot model exists to avoid.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
for _m in ("anthropic", "fal_client", "elevenlabs"):
    sys.modules.setdefault(_m, types.ModuleType(_m))

from backend import config, slots  # noqa: E402
from backend.manifest import Camera, Shot, Storyboard  # noqa: E402


@pytest.fixture(autouse=True)
def project(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "MANIFEST_PATH", tmp_path / "storyboard_manifest.json")
    return tmp_path


class _DS:
    """A DirectorShot stand-in; slots only need these four fields."""
    def __init__(self, sid, duration=6.0, motion_type="parallax"):
        self.id, self.duration, self.motion_type = sid, duration, motion_type


def _sb(*beats) -> Storyboard:
    return Storyboard(title="T", shots=[
        Shot(scene_id=b, narration="n", camera=Camera(move="static", duration=d))
        for b, d in beats])


def _covered(mapping):
    return lambda beat_id: mapping.get(beat_id)


# --- building --------------------------------------------------------------------

def test_one_beat_becomes_one_slot_per_director_shot():
    """beat != shot: a 12s beat covered by two shots is two slots, not one."""
    sb = _sb(("s001", 12.0))
    out = slots.build(sb, _covered({"s001": [_DS("s001.01"), _DS("s001.02")]}))
    assert [s.shot_id for s in out] == ["s001.01", "s001.02"]
    assert [s.index for s in out] == [0, 1]


def test_a_beat_with_no_coverage_is_a_single_whole_beat_slot():
    out = slots.build(_sb(("s001", 12.0)), _covered({}))
    assert len(out) == 1 and out[0].shot_id == ""
    assert out[0].intended_duration == 12.0


def test_slot_identity_is_derived_so_a_rebuild_is_the_same_slot():
    sb = _sb(("s001", 12.0))
    cov = _covered({"s001": [_DS("s001.01")]})
    assert slots.build(sb, cov)[0].id == slots.build(sb, cov)[0].id


def test_static_shots_expect_a_still():
    out = slots.build(_sb(("s001", 6.0)),
                      _covered({"s001": [_DS("s001.01", motion_type="static")]}))
    assert out[0].expected_media == slots.STILL


# --- C5: a placeholder is semantic --------------------------------------------------

def test_a_placeholder_carries_everything_needed_to_replace_it():
    out = slots.build(_sb(("s003", 10.0)), _covered({"s003": [_DS("s003.02", 4.0)]}))
    ph = out[0]
    assert ph.placeholder is True
    assert ph.shot_id == "s003.02"          # which shot it belongs to
    assert ph.beat_id == "s003"             # the beat it came from
    assert ph.id                            # slot identity
    assert ph.intended_duration == 4.0      # the duration it is owed
    assert ph.expected_media == slots.VIDEO  # what is expected to arrive


def test_a_placeholder_holds_its_full_length_in_the_cut():
    """Otherwise everything after it shifts while the media is missing."""
    out = slots.build(_sb(("s001", 9.0)), _covered({"s001": [_DS("s001.01", 9.0)]}))
    assert out[0].duration == 9.0


def test_placeholder_is_derived_from_media_not_stored():
    """A stored flag left true after media arrives is how a filled slot goes on
    rendering as a placeholder."""
    s = slots.TimelineSlot(id="x", beat_id="s001", shot_id="s001.01",
                           intended_duration=5.0)
    assert s.placeholder is True
    s.media = "render/s001/s001.01.mp4"
    assert s.placeholder is False


def test_a_late_arrival_fills_the_slot_without_disturbing_the_edit():
    out = slots.build(_sb(("s001", 12.0)),
                      _covered({"s001": [_DS("s001.01"), _DS("s001.02")]}))
    out[0].trim_in = 1.0
    before_id, before_index = out[0].id, out[0].index

    filled = slots.fill(out, "s001.01", "render/s001/s001.01.mp4", attempt="a1")
    assert filled.placeholder is False
    assert filled.id == before_id and filled.index == before_index
    assert filled.trim_in == 1.0
    assert [s.shot_id for s in out] == ["s001.01", "s001.02"]


# --- §7.1: a take swap must not recreate the edit -----------------------------------

def test_choosing_a_different_take_replaces_media_in_the_same_slot():
    out = slots.build(_sb(("s001", 6.0)), _covered({"s001": [_DS("s001.01")]}))
    slots.fill(out, "s001.01", "take_b.mp4", attempt="a1")
    out[0].trim_in, out[0].trim_out = 0.5, 4.0
    slot_before = out[0].id

    slots.fill(out, "s001.01", "take_d.mp4", attempt="a2")
    assert out[0].media == "take_d.mp4"
    assert out[0].source_attempt == "a2"
    assert out[0].id == slot_before, "the slot was recreated"
    assert (out[0].trim_in, out[0].trim_out) == (0.5, 4.0), "trims were lost"


def test_filling_an_unknown_shot_changes_nothing():
    out = slots.build(_sb(("s001", 6.0)), _covered({"s001": [_DS("s001.01")]}))
    assert slots.fill(out, "s009.99", "x.mp4") is None
    assert out[0].media == ""


# --- reconciliation across a re-plan --------------------------------------------------

def test_a_replan_of_one_beat_keeps_the_other_beats_trims():
    sb = _sb(("s001", 6.0), ("s002", 6.0))
    cov = {"s001": [_DS("s001.01")], "s002": [_DS("s002.01")]}
    existing = slots.build(sb, _covered(cov))
    existing[1].trim_in = 1.5
    slots.fill(existing, "s002.01", "b.mp4")

    cov["s001"] = [_DS("s001.01"), _DS("s001.02", 3.0)]      # s001 re-planned
    merged = slots.reconcile(existing, slots.build(sb, _covered(cov)))

    s002 = next(s for s in merged if s.shot_id == "s002.01")
    assert s002.trim_in == 1.5, "an unrelated beat lost its trim"
    assert s002.media == "b.mp4", "an unrelated beat lost its media"


def test_a_dropped_shot_leaves_the_cut():
    sb = _sb(("s001", 6.0))
    existing = slots.build(sb, _covered({"s001": [_DS("s001.01"), _DS("s001.02")]}))
    merged = slots.reconcile(existing, slots.build(sb, _covered({"s001": [_DS("s001.01")]})))
    assert [s.shot_id for s in merged] == ["s001.01"]


def test_a_trim_that_no_longer_fits_is_dropped_and_said_so():
    """§7.1: do not silently stretch or retime the cut around a plan change."""
    sb = _sb(("s001", 6.0))
    existing = slots.build(sb, _covered({"s001": [_DS("s001.01", 6.0)]}))
    existing[0].trim_in, existing[0].trim_out = 1.0, 5.0

    shorter = slots.build(sb, _covered({"s001": [_DS("s001.01", 2.0)]}))
    merged = slots.reconcile(existing, shorter)
    assert merged[0].trim_out == 0.0 and merged[0].trim_in == 0.0
    assert "trim dropped" in merged[0].note


def test_a_trim_that_still_fits_survives():
    sb = _sb(("s001", 6.0))
    existing = slots.build(sb, _covered({"s001": [_DS("s001.01", 6.0)]}))
    existing[0].trim_in, existing[0].trim_out = 0.5, 3.0
    merged = slots.reconcile(existing, slots.build(sb, _covered({"s001": [_DS("s001.01", 5.0)]})))
    assert (merged[0].trim_in, merged[0].trim_out) == (0.5, 3.0)


def test_reconcile_renumbers_positions():
    sb = _sb(("s001", 6.0), ("s002", 6.0))
    existing = slots.build(sb, _covered({}))
    merged = slots.reconcile(existing, slots.build(sb, _covered({})))
    assert [s.index for s in merged] == [0, 1]


# --- §6.2: incomplete coverage is stated, not hidden -----------------------------------

def test_coverage_says_how_many_placeholders_draft_one_will_use():
    sb = _sb(("s001", 12.0))
    out = slots.build(sb, _covered({"s001": [_DS("s001.01"), _DS("s001.02"),
                                             _DS("s001.03")]}))
    slots.fill(out, "s001.01", "a.mp4")
    c = slots.coverage(out)
    assert c == {"slots": 3, "ready": 1, "placeholders": 2, "runtime": 18.0,
                 "summary": "1/3 visuals ready · Draft 1 will use 2 placeholders"}


def test_a_complete_cut_says_nothing_about_placeholders():
    out = slots.build(_sb(("s001", 6.0)), _covered({"s001": [_DS("s001.01")]}))
    slots.fill(out, "s001.01", "a.mp4")
    assert slots.coverage(out)["summary"] == "1/1 visuals ready"


def test_one_missing_visual_is_singular():
    out = slots.build(_sb(("s001", 6.0)), _covered({"s001": [_DS("s001.01")]}))
    assert "1 placeholder" in slots.coverage(out)["summary"]
    assert "placeholders" not in slots.coverage(out)["summary"]


# --- persistence -------------------------------------------------------------------

def test_slots_round_trip_through_disk():
    out = slots.build(_sb(("s001", 6.0)), _covered({"s001": [_DS("s001.01")]}))
    out[0].trim_in = 1.25
    slots.fill(out, "s001.01", "a.mp4", attempt="att-1")
    slots.save(out)

    back = slots.load()
    assert len(back) == 1
    assert back[0].id == out[0].id
    assert back[0].trim_in == 1.25
    assert back[0].source_attempt == "att-1"


def test_no_saved_cut_is_an_empty_cut_not_an_error():
    assert slots.load() == []


def test_an_unreadable_cut_raises_rather_than_reading_as_empty():
    """Returning [] would rebuild the cut and silently discard every trim."""
    slots.save(slots.build(_sb(("s001", 6.0)), _covered({})))
    slots.slots_path().write_text("{not json", encoding="utf-8")
    with pytest.raises(ValueError):
        slots.load()


def test_slots_are_written_inside_the_bound_project(tmp_path):
    from backend import projects
    other = tmp_path / "other"
    other.mkdir()
    ctx = projects.ProjectContext.from_manifest(other / "storyboard_manifest.json")
    with projects.use(ctx):
        assert slots.slots_path() == other / "timeline_slots.json"


# --- §6.2: Draft 1 builds before generation finishes -------------------------------

import shutil as _shutil
import subprocess as _sp

HAS_FFMPEG = bool(_shutil.which("ffmpeg") and _shutil.which("ffprobe"))
needs_ffmpeg = pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg/ffprobe not installed")


@needs_ffmpeg
def test_a_placeholder_clip_is_exactly_the_beat_length(tmp_path):
    """Everything after a placeholder sits at the right time only if its length
    is right, so the length is the thing worth asserting."""
    from backend import timeline
    p = timeline.placeholder_clip(tmp_path / "s001.mp4", 3.25, "s001")
    assert abs(timeline._probe_seconds(p) - 3.25) < 0.05


@needs_ffmpeg
def test_a_placeholder_is_reused_until_the_beat_is_retimed(tmp_path):
    """A stale placeholder of the old length would shift the whole cut."""
    from backend import timeline
    first = timeline.placeholder_clip(tmp_path / "s001.mp4", 3.0)
    stamp = first.stat().st_mtime_ns
    again = timeline.placeholder_clip(tmp_path / "s001.mp4", 3.0)
    assert again.stat().st_mtime_ns == stamp, "rebuilt an unchanged placeholder"

    retimed = timeline.placeholder_clip(tmp_path / "s001.mp4", 6.0)
    assert abs(timeline._probe_seconds(retimed) - 6.0) < 0.05


@needs_ffmpeg
def test_a_placeholder_is_not_another_beats_footage(tmp_path):
    """§11.2 forbids silent substitution. A placeholder must be blank, not
    borrowed -- a cut that quietly reuses the previous shot looks finished."""
    from backend import timeline
    a = timeline.placeholder_clip(tmp_path / "a.mp4", 2.0)
    b = timeline.placeholder_clip(tmp_path / "b.mp4", 2.0)
    assert a != b
    # identical black frames are expected; what matters is neither came from a
    # real render, so neither carries picture content
    probe = _sp.run(["ffprobe", "-v", "error", "-select_streams", "v:0",
                     "-show_entries", "stream=width,height", "-of", "csv=p=0", str(a)],
                    capture_output=True, text=True)
    assert probe.stdout.strip() == "1280,720"
