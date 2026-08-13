"""Unit tests for backend/manifest.py: the Gate 1 predicate, and load/save.

`Storyboard.gate_cleared()` is the whole of Gate 1 in code. pipeline.py branches
on it twice (pipeline.py:174, pipeline.py:188) and main.py reports it to the
studio (backend/main.py:3847), and until now nothing exercised it directly — every
existing test that touches a paid shot builds one and then tests something else.
A predicate that decides whether the pipeline may call a paid video API is worth
pinning term by term, because each term fails silently: drop the `video_model`
check and the gate opens on a shot with no model to render with; drop the
`storyboard_approved` check and it opens with no human in the loop at all.

The second half covers `load()`/`save()`, the local-JSON path. Both are also
exercised end-to-end by tests/test_project_isolation.py, but only for "does it
write to the right project" — the empty/absent/`{}` fallbacks in load()'s
docstring, and the `from_dict` key-filtering that keeps one stray field from
costing a whole storyboard, had no test.

Field-by-field to_dict -> from_dict survival lives in test_manifest_roundtrip.py
and is deliberately not duplicated here; what this file round-trips is the parts
that file skips — the MotionType enum, and a trip through real JSON on disk.

Every file operation goes through tmp_path. Nothing here touches the repo's own
storyboard_manifest.json, config's process globals, or the network.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend import manifest as M  # noqa: E402
from backend.manifest import (  # noqa: E402
    AudioLayer,
    Camera,
    MotionType,
    RenderConfig,
    Shot,
    Storyboard,
)


def _paid(scene_id: str, *, approved: bool = True,
          video_model: str | None = "seedance_2_0") -> Shot:
    """An AI_VIDEO (Tier C, paid) beat, gate-clearing unless told otherwise."""
    return Shot(scene_id=scene_id, motion_type=MotionType.AI_VIDEO,
                approved=approved, video_model=video_model)


def _free(scene_id: str, motion_type: MotionType = MotionType.PARALLAX) -> Shot:
    """A local-render beat. Deliberately left unapproved and without a video
    model: neither is a cost, so neither may hold the gate shut."""
    return Shot(scene_id=scene_id, motion_type=motion_type)


# How the mixed-list gate tests are shaped, and why they are shaped that way.
#
# Three revisions of this file were each defeated by a mutation keyed to the
# shape of its own fixture, and the pattern only became visible on the third:
#
#   1. one paid shot            -> `any()` on the model term passed (with one
#                                  shot, `all` and `any` agree)
#   2. three shots, ends only   -> a gate ignoring index 1 when len(paid) == 3
#                                  passed all of them
#   3. three shots, all indices -> `for s in self.shots[:3]` passed all 15 gate
#                                  tests, and opened Gate 1 on a fourth beat
#                                  with no model and no approval
#
# Each fix closed its instance and left the class open, because a fixture of
# fixed size N cannot distinguish a gate that reads the whole list from one that
# reads a prefix of length >= N. Adding a four-shot case would just move the
# mutation to `[:4]`.
#
# So both dimensions are parametrized and both are derived: the paid-list LENGTH
# over 1.._MAX_PAID, crossed with EVERY position in a list of that length. That
# closes every fixed-prefix and fixed-index weakening in one change rather than
# one more instance of it. Raising _MAX_PAID widens both dimensions together;
# nothing here is hand-written per-length.
#
# _MAX_PAID is 6, not 5, and the reason is worth stating because it is easy to
# get wrong by one. A `shots[:k]` gate is only detectable when some case puts a
# BROKEN paid beat past position k, so a pair (n, i) kills it only when i >= k,
# and a list of n paid beats therefore catches prefixes up to k = n - 1. Each
# surviving pair costs 3 collected items: one from the unapproved test, two from
# the video_model test's [None, ""] cross. Nothing else fails under truncation,
# because every other gate assertion is `is True` and a shorter list only makes
# the gate more permissive.
#
# So the kill counts are 3x the triangular numbers, and measured they are:
#
#   _MAX_PAID = 5:  [:1] 30   [:2] 18   [:3]  9   [:4] 3   [:5] GREEN
#   _MAX_PAID = 6:  [:1] 45   [:2] 30   [:3] 18   [:4] 9   [:5] 3   [:6] GREEN
#
# The five-beat row is why this is 6: its longest list is five paid beats plus
# one free, so `[:5]` truncates only the free one and passes. The agreed
# boundary is that `[:6]` is out of scope -- neither a plausible accident nor a
# plausible regression -- and everything below it is in, so the list has to
# reach six. At 6 the first green column is exactly `[:6]`.
_MAX_PAID = 6

# (list length, index broken) for every position of every length in 1.._MAX_PAID.
_LENGTHS_X_POSITIONS = [(n, i) for n in range(1, _MAX_PAID + 1) for i in range(n)]
_LXP_IDS = [f"{n}paid-break{i}" for n, i in _LENGTHS_X_POSITIONS]


def _paid_list(n: int) -> list[Shot]:
    """``n`` gate-clearing paid beats — the list each mixed test breaks one of."""
    return [_paid(f"s{i + 1:03d}") for i in range(n)]


def _siblings_are_ready(shots: list[Shot], broken_at: int) -> bool:
    """Every shot except ``broken_at`` clears the gate on its own.

    The mixed-list tests assert a *closed* gate, which is what a broken helper
    would also produce — if `_paid()` stopped building a gate-clearing beat they
    would pass for the wrong reason and stop testing the quantifier at all.
    This pins that exactly one beat in the list is the one holding it shut."""
    rest = [s for i, s in enumerate(shots) if i != broken_at]
    return Storyboard(title="T", storyboard_approved=True,
                      shots=rest).gate_cleared() is True


# --- Gate 1: Storyboard.gate_cleared() ----------------------------------------

def test_gate_shut_when_the_storyboard_is_not_approved():
    """The human approval flag is checked first and alone. Every shot below is
    otherwise ready, so if this passes it is that flag doing the work."""
    sb = Storyboard(title="T", storyboard_approved=False,
                    shots=[_paid("s001"), _free("s002")])
    assert sb.gate_cleared() is False


@pytest.mark.parametrize("n_paid,broken_at", _LENGTHS_X_POSITIONS, ids=_LXP_IDS)
def test_gate_shut_when_any_paid_shot_is_unapproved(n_paid, broken_at):
    """Approving the storyboard is not approving the spend. Each paid beat is
    its own line item and needs its own tick — and the quantifier is `all`, so
    one unticked beat holds the gate however ready its neighbours are, and
    wherever in the list it sits.

    Every position of every length up to _MAX_PAID; see the note there for why
    a fixed-size fixture is not enough."""
    paid = _paid_list(n_paid)
    paid[broken_at] = _paid(paid[broken_at].scene_id, approved=False)
    sb = Storyboard(title="T", storyboard_approved=True,
                    shots=[*paid, _free("s900")])
    assert sb.gate_cleared() is False
    assert _siblings_are_ready(paid, broken_at)


@pytest.mark.parametrize("video_model", [None, ""])
@pytest.mark.parametrize("n_paid,broken_at", _LENGTHS_X_POSITIONS, ids=_LXP_IDS)
def test_gate_shut_when_any_one_of_several_paid_shots_has_no_video_model(
        video_model, n_paid, broken_at):
    """The model term is quantified over *every* paid beat, at every length.

    `bool(video_model)` is the test, so an unset model and a blank string both
    hold the gate: approving a Tier-C beat with nothing to render it on would
    send the pipeline at a paid API with no model selected. The `n_paid=1` cases
    pin that minimal form; the longer ones pin the quantifier, which one shot
    cannot show because `all` and `any` agree over a single element.

    Weaken the predicate to `... and any(bool(s.video_model) for paid)` — all
    paid shots approved, at least one carrying a model — and a storyboard whose
    second Tier-C beat has no model clears Gate 1 and reaches the paid API with
    nothing to render on. That is the silent gate weakening this file exists to
    catch. See the note on _MAX_PAID for why the length is parametrized too."""
    paid = _paid_list(n_paid)
    paid[broken_at] = _paid(paid[broken_at].scene_id, video_model=video_model)
    sb = Storyboard(title="T", storyboard_approved=True,
                    shots=[*paid, _free("s900")])
    assert sb.gate_cleared() is False
    assert _siblings_are_ready(paid, broken_at)


def test_gate_open_when_approved_and_every_paid_shot_is_ready():
    sb = Storyboard(title="T", storyboard_approved=True,
                    shots=[_paid("s001"), _paid("s002"), _free("s003")])
    assert sb.gate_cleared() is True


def test_gate_open_with_no_paid_shots_even_though_no_shot_is_approved():
    """The `all(...)` is vacuously true over an empty paid set, and that is the
    intended reading: an all-local episode costs nothing at the video API, so
    per-shot budget approval has nothing to gate. Storyboard approval still
    applies — see the first test — and Gate 2 still reviews the cut."""
    sb = Storyboard(title="T", storyboard_approved=True,
                    shots=[_free("s001", MotionType.STATIC),
                           _free("s002", MotionType.PARALLAX)])
    assert not any(s.approved for s in sb.shots)
    assert sb.gate_cleared() is True


def test_gate_open_on_an_empty_storyboard_is_approval_only():
    sb = Storyboard(title="T", storyboard_approved=True, shots=[])
    assert sb.gate_cleared() is True
    assert Storyboard(title="T", shots=[]).gate_cleared() is False


# --- paid_shots() / needs_paid_video() ----------------------------------------

def test_paid_shots_selects_only_ai_video_beats():
    """backend/main.py:1083 and backend/main.py:3149 report len(paid_shots()) as the spend
    estimate, so a Tier-A/B beat leaking in here misquotes the budget."""
    sb = Storyboard(title="T", shots=[
        _free("s001", MotionType.STATIC),
        _paid("s002"),
        _free("s003", MotionType.PARALLAX),
        _paid("s004", approved=False, video_model=None),
    ])
    assert [s.scene_id for s in sb.paid_shots()] == ["s002", "s004"]


def test_paid_shots_ignores_approval_and_model():
    """It answers "what costs money", not "what is ready" — the unapproved,
    model-less beat above is still in the list. gate_cleared() is the readiness
    question; conflating the two would let an unapproved beat vanish from the
    budget line instead of blocking it."""
    sb = Storyboard(title="T", shots=[_paid("s001", approved=False,
                                            video_model=None)])
    assert len(sb.paid_shots()) == 1


@pytest.mark.parametrize("motion_type,paid", [
    (MotionType.STATIC, False),
    (MotionType.PARALLAX, False),
    (MotionType.AI_VIDEO, True),
])
def test_needs_paid_video_covers_every_motion_type(motion_type, paid):
    """Enumerated rather than spot-checked, so adding a fourth tier fails here
    instead of silently defaulting to free."""
    assert Shot(scene_id="s001", motion_type=motion_type).needs_paid_video() is paid


def test_every_motion_type_is_classified():
    """Guards the parametrize above against a tier added without a verdict."""
    covered = {MotionType.STATIC, MotionType.PARALLAX, MotionType.AI_VIDEO}
    assert set(MotionType) == covered, (
        f"MotionType gained {sorted(set(MotionType) - covered)}. Decide whether "
        f"it is paid and add it to test_needs_paid_video_covers_every_motion_type."
    )


# --- from_dict: the enum, and tolerance of imperfect input --------------------

def _roundtrip(sb: Storyboard) -> Storyboard:
    d = sb.to_dict()
    return Storyboard.from_dict(sb.id or "probe", d, d.get("shots") or [])


def test_motion_type_survives_as_an_enum_not_a_string():
    """to_dict() flattens the enum to its value for JSON; from_dict must rebuild
    it.

    What does *not* break is the gate. MotionType is declared
    `class MotionType(str, Enum)` (backend/manifest.py:28), and that mixin makes
    `"ai_video" == MotionType.AI_VIDEO` true, so a shot left holding the raw
    string still reports needs_paid_video() is True and is still counted by
    paid_shots(). The mixin is exactly what keeps Gate 1 tolerant of an
    unconverted value — so this test is not what stands between a raw string and
    the paid API, and must not be read as if it were.

    What breaks is everything that asks for the member rather than its value:
    `is` identity, isinstance(x, MotionType), and any call site reaching for
    `.value` or `.name` (`"ai_video".value` is an AttributeError). Those are
    real and worth pinning, which is why the assertions below use `is`."""
    sb = Storyboard(title="T", shots=[_paid("s001"),
                                      _free("s002", MotionType.STATIC)])
    assert sb.to_dict()["shots"][0]["motion_type"] == "ai_video"

    back = _roundtrip(sb)
    assert back.shots[0].motion_type is MotionType.AI_VIDEO
    assert back.shots[1].motion_type is MotionType.STATIC
    # The three things a surviving raw string would actually fail, stated
    # directly so the test matches its rationale rather than the gate's.
    assert isinstance(back.shots[0].motion_type, MotionType)
    assert back.shots[0].motion_type.value == "ai_video"
    assert back.shots[0].motion_type.name == "AI_VIDEO"


def test_missing_motion_type_defaults_to_parallax():
    """Tier B is ~70% of shots and costs nothing, so it is the safe default: a
    beat with no tier recorded must never default into the paid tier."""
    back = Storyboard.from_dict("p", {}, [{"scene_id": "s001"}])
    assert back.shots[0].motion_type is MotionType.PARALLAX
    assert back.shots[0].needs_paid_video() is False


def test_missing_camera_becomes_a_default_camera():
    back = Storyboard.from_dict("p", {}, [{"scene_id": "s001"}])
    assert back.shots[0].camera == Camera()


def test_non_dict_camera_becomes_a_default_camera():
    """`isinstance(raw_cam, dict)` — a null or a stray scalar falls back rather
    than raising, for the same reason as the key filtering below."""
    back = Storyboard.from_dict("p", {}, [{"scene_id": "s001", "camera": None},
                                          {"scene_id": "s002", "camera": "push_in"}])
    assert back.shots[0].camera == Camera()
    assert back.shots[1].camera == Camera()


def test_unknown_keys_are_dropped_rather_than_raising():
    """Shot(**shot) used to explode on any extra field, and get_current_project
    catches that and falls through to "create a fresh project" — so one stray
    key was enough to overwrite a whole storyboard with an empty one. Camera and
    every nested config filter for the same reason."""
    back = Storyboard.from_dict(
        "p",
        {"title": "T", "render": {"backend": "flux-cfg", "from_the_future": 1},
         "mix": {"sfx": 0.42, "unknown": 1},
         "motion": {"speed": 2.0, "unknown": 1},
         "grade": {"contrast": 0.3, "unknown": 1}},
        [{"scene_id": "s001", "narration": "n", "who_added_this": True,
          "camera": {"move": "pan_left", "bogus": 1}}],
    )
    assert back.shots[0].scene_id == "s001"
    assert back.shots[0].narration == "n"
    assert back.shots[0].camera.move == "pan_left"
    assert back.render.backend == "flux-cfg"
    assert back.mix.sfx == 0.42
    assert back.motion.speed == 2.0
    assert back.grade.contrast == 0.3


def test_unknown_keys_inside_an_sfx_layer_are_dropped():
    """AudioLayer filters its own keys, the same way Shot and Camera do."""
    back = Storyboard.from_dict("p", {}, [{
        "scene_id": "s001",
        "sfx_layers": [{"id": "a", "prompt": "wind", "gain": 0.5,
                        "from_the_future": 1}],
    }])
    assert len(back.shots[0].sfx_layers) == 1
    assert back.shots[0].sfx_layers[0].prompt == "wind"
    assert back.shots[0].sfx_layers[0].gain == 0.5


# `sfx_layers` is the one nested collection from_dict rebuilds without checking
# its shape (backend/manifest.py:282-286). `camera` two lines below it gets an
# isinstance(raw_cam, dict) guard; this does not. A malformed layer therefore
# fails in one of two ways, and the split is `(lay or {})`:
#
#   truthy non-mapping ("wind", 1)  -> reaches .items(), AttributeError out of
#       from_dict, into the same get_current_project "create a fresh project"
#       fallback the key filtering exists to prevent. Loud, and costs the board.
#   falsy   (None, "", 0, [], {})   -> absorbed by `or {}` and rebuilt as a
#       DEFAULT AudioLayer(). Silent, and costs the beat's ambience.
#
# The falsy half is the dangerous one, which is why it is pinned below rather
# than left to the "it raises" summary. A phantom default layer is truthy, and
# resolve_sfx_layers (backend/audio.py:244-246) reads
# `layers = list(... or []); if layers: return layers` -- so the phantom
# short-circuits the legacy `<scene>.mp3` / `sfx`-prompt fallback underneath it
# and the beat loses ambience it already had, with nothing raised and nothing
# logged. Measured: a beat with a real s001.mp3 resolves to [('legacy',
# 's001.mp3')] normally and to [('', '')] once one None sits in sfx_layers.
#
# Both halves are recorded the same way as the unrecognised motion_type below:
# what happens today, plus a strict xfail for what should.

@pytest.mark.parametrize("sfx_layers", [
    {"a": 1},          # a mapping of layers rather than a list
    ["wind"],          # a list of bare prompt strings
    "wind",            # a single prompt string
    [1],               # a list of bare numbers
])
def test_a_truthy_misshapen_sfx_layers_currently_raises(sfx_layers):
    """Current behaviour of the loud half — see the hazard note above."""
    with pytest.raises(AttributeError):
        Storyboard.from_dict("p", {}, [{"scene_id": "s001",
                                        "sfx_layers": sfx_layers}])


@pytest.mark.parametrize("layer", [None, "", 0, [], {}])
def test_a_falsy_sfx_layer_currently_becomes_a_phantom_default(layer):
    """Current behaviour of the silent half, recorded — not endorsed.

    `or {}` turns each of these into `AudioLayer()`: a layer with no file, no
    prompt and unity gain, indistinguishable from a real one to anything that
    only checks whether the list is empty. That is precisely what
    resolve_sfx_layers checks, so this is the value that quietly deletes a
    beat's ambience. Pinned so the summary above cannot drift back to "malformed
    layers raise", which would send a maintainer looking for an exception that
    never comes."""
    back = Storyboard.from_dict("p", {}, [{"scene_id": "s001",
                                           "sfx_layers": [layer]}])
    assert back.shots[0].sfx_layers == [AudioLayer()]


@pytest.mark.parametrize("sfx_layers", [["wind"], [None]], ids=["truthy", "falsy"])
@pytest.mark.xfail(strict=True, reason=(
    "KNOWN GAP in backend/manifest.py: from_dict rebuilds sfx_layers without a "
    "shape check. A truthy non-mapping layer raises AttributeError and costs the "
    "whole storyboard; a falsy one is absorbed by `(lay or {})` into a phantom "
    "default AudioLayer(), which is truthy and so suppresses the legacy "
    "ambience fallback in resolve_sfx_layers -- silently. `camera` guards with "
    "isinstance; this does not. Deferred - the fix is production code and this "
    "PR is tests-only. Remove this marker when from_dict drops layers it cannot "
    "read, BOTH halves: fixing only the truthy one leaves the silent case live."))
def test_an_unreadable_sfx_layer_should_be_dropped_not_fatal_and_not_phantom(
        sfx_layers):
    """The regression test for the fix we owe, kept red until it lands.

    Ambience is the most disposable thing in the manifest — losing a layer costs
    a sound, losing the storyboard costs the episode, and losing it silently
    costs the sound plus the chance to notice. An unreadable layer should drop,
    leaving the rest of the shot standing and the list genuinely empty so the
    legacy fallback still fires.

    Parametrized over both halves deliberately: implement the drop for truthy
    layers only and this case XPASSes (strict, so it fails and demands
    attention) while `[None]` stays red, instead of the whole marker coming off
    over a half-fix."""
    back = Storyboard.from_dict("p", {}, [{
        "scene_id": "s001", "narration": "n", "sfx_layers": sfx_layers}])
    assert back.shots[0].narration == "n"
    assert back.shots[0].sfx_layers == []


def test_render_config_survives_a_round_trip():
    sb = Storyboard(title="T", render=RenderConfig(
        backend="flux-cfg", variations=5, video_model="kling_v3",
        nag_scale=7.5, negative_prompt="no photographs"))
    assert _roundtrip(sb).render == sb.render


def test_camera_survives_a_round_trip():
    sb = Storyboard(title="T", shots=[Shot(
        scene_id="s001",
        camera=Camera(move="pan_left", duration=12.5, speed=1.5,
                      amount=0.2, duration_locked=True))])
    assert _roundtrip(sb).shots[0].camera == sb.shots[0].camera


# An unknown motion_type *value* is a live hazard, not a settled contract.
# Unknown *keys* are filtered out precisely so one stray field cannot cost a
# whole storyboard, but `MotionType(shot.get("motion_type", "parallax"))` still
# raises ValueError on an unrecognised value — and that lands in the very
# get_current_project fallback the key filtering exists to avoid, discarding a
# valid storyboard to "create a fresh project".
#
# Fixing it means changing backend/manifest.py, and this suite is tests-only, so
# the two tests below split the job rather than blessing the current behaviour:
# the first records what the code does today (so a change in the *shape* of the
# failure is noticed), and the second states what it should do and is expected to
# fail until someone makes it true. strict=True means the day the production fix
# lands, the xfail turns into an XPASS failure and forces both of these to be
# rewritten deliberately, instead of the desired-behaviour test quietly passing
# unnoticed.

def test_an_unrecognised_motion_type_currently_raises():
    """Current behaviour, recorded — see the hazard note above. Not an
    endorsement: the companion xfail below is the behaviour we want."""
    with pytest.raises(ValueError):
        Storyboard.from_dict("p", {}, [{"scene_id": "s001",
                                        "motion_type": "claymation"}])


@pytest.mark.xfail(strict=True, reason=(
    "KNOWN GAP in backend/manifest.py: an unrecognised motion_type value raises "
    "out of from_dict instead of falling back, so one bad value can cost the "
    "whole storyboard. Deferred - the fix is production code and this PR is "
    "tests-only. Remove this marker when from_dict degrades gracefully."))
def test_an_unrecognised_motion_type_should_fall_back_to_parallax():
    """The regression test for the fix we owe, kept red until it lands.

    An unreadable tier is not a reason to lose an episode. It should degrade the
    way a missing tier already does — to PARALLAX, the free tier — because the
    safe failure for a value nobody recognises is the one that cannot spend
    money. The rest of the shot must survive intact; that is the whole point of
    degrading instead of raising."""
    back = Storyboard.from_dict("p", {}, [{"scene_id": "s001",
                                           "narration": "n",
                                           "motion_type": "claymation"}])
    assert back.shots[0].motion_type is MotionType.PARALLAX
    assert back.shots[0].needs_paid_video() is False
    assert back.shots[0].narration == "n"


# --- load() / save() on local JSON --------------------------------------------

def test_save_then_load_round_trips_through_real_json(tmp_path):
    path = tmp_path / "nested" / "storyboard_manifest.json"   # parent must be created
    sb = Storyboard(title="The Wendigo", channel="bestiary",
                    cultural_origin="Algonquian", storyboard_approved=True,
                    render=RenderConfig(backend="flux-cfg", variations=4),
                    shots=[_paid("s001"),
                           Shot(scene_id="s002", narration="n",
                                camera=Camera(move="pan_left", duration=9.0))])
    M.save(sb, path)

    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert on_disk["shots"][0]["motion_type"] == "ai_video", "enum must be JSON-safe"

    back = M.load(path)
    assert back.title == "The Wendigo"
    assert back.cultural_origin == "Algonquian"
    assert back.storyboard_approved is True
    assert back.render.backend == "flux-cfg" and back.render.variations == 4
    assert [s.scene_id for s in back.shots] == ["s001", "s002"]
    assert back.shots[0].motion_type is MotionType.AI_VIDEO
    assert back.shots[1].camera.move == "pan_left"
    assert back.shots[1].camera.duration == 9.0
    assert back.gate_cleared() is True, "the gate verdict must survive a restart"


def test_save_leaves_no_temp_file_behind(tmp_path):
    """save() writes to <path>.tmp and renames. A leftover .tmp means the rename
    did not happen and the manifest on disk is the previous one."""
    path = tmp_path / "storyboard_manifest.json"
    M.save(Storyboard(title="T"), path)
    assert path.exists()
    assert list(tmp_path.iterdir()) == [path]


def test_save_overwrites_rather_than_appending(tmp_path):
    path = tmp_path / "storyboard_manifest.json"
    M.save(Storyboard(title="first", shots=[_free("s001"), _free("s002")]), path)
    M.save(Storyboard(title="second", shots=[_free("s001")]), path)
    back = M.load(path)
    assert back.title == "second"
    assert len(back.shots) == 1


@pytest.mark.parametrize("contents", [None, "", "   \n", "{}"])
def test_load_yields_a_fresh_storyboard_for_absent_or_empty_files(tmp_path, contents):
    """load()'s documented contract: "an empty or absent file yields a fresh
    Storyboard". It must not raise — a JSONDecodeError here would abort a run on
    a half-written or never-written manifest — and the fresh board must be shut
    at the gate, never open by default."""
    path = tmp_path / "storyboard_manifest.json"
    if contents is not None:
        path.write_text(contents, encoding="utf-8")

    sb = M.load(path)
    assert sb.title == ""
    assert sb.shots == []
    assert sb.storyboard_approved is False
    assert sb.script_locked is False
    assert sb.gate_cleared() is False, "a blank manifest must never clear Gate 1"
    assert sb.render == RenderConfig()


def test_load_never_touches_the_default_manifest_when_given_a_path(tmp_path, monkeypatch):
    """Every call here passes an explicit path. This asserts that is sufficient:
    load() must not fall back to config.manifest_path() and read another
    project's board — the §11.3 cross-project failure the path argument exists
    to prevent."""
    from backend import config

    def _boom() -> Path:
        raise AssertionError("load() consulted the process-global manifest path")

    monkeypatch.setattr(config, "manifest_path", _boom)
    path = tmp_path / "storyboard_manifest.json"
    M.save(Storyboard(title="explicit"), path)
    assert M.load(path).title == "explicit"


def test_load_derives_the_project_id_from_the_path(tmp_path):
    """Documenting current behaviour: the `id` written into the file is ignored
    and the loaded id is derived from the path, with every non-alphanumeric
    character replaced by an underscore. Worth pinning because save_project()
    raises without an id.

    The invariant asserted is `c.isalnum() or c == "_"`, which is exactly what
    load() implements -- NOT an ASCII whitelist. str.isalnum() is true for
    Unicode letters, so load() retains them, and tmp_path inherits the system
    temp location: on a machine whose username is "José" the derived id legally
    contains "é" and an ASCII assertion would fail in a perfectly valid
    environment. The separator case below pins the transformation itself on a
    path segment this test controls, so it holds whatever the username is."""
    path = tmp_path / "ep 001" / "storyboard_manifest.json"
    M.save(Storyboard(id="written-into-the-file", title="T"), path)

    loaded = M.load(path)
    assert loaded.id != "written-into-the-file"
    assert loaded.id
    assert all(c.isalnum() or c == "_" for c in loaded.id), (
        f"derived id {loaded.id!r} contains a character load() should have "
        f"replaced with an underscore"
    )
    # Deterministic regardless of the machine: the separator and the space in
    # the segment this test created must both come back as underscores.
    assert "ep_001_storyboard_manifest_json" in loaded.id
    assert M.load(path).id == loaded.id, "the derived id must be stable"
