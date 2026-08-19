"""Spike C + planner tests. All offline — no API calls, no fal, no ffmpeg."""

from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.modules.setdefault("anthropic", types.ModuleType("anthropic"))

from backend import capabilities as cap  # noqa: E402
from backend import planner as P  # noqa: E402


# --- capabilities: the constraints that used to live in prose ------------------

def test_veo_only_accepts_4_6_8():
    """The constraint that broke coverage planning, now machine-readable."""
    assert cap.legal_durations("veo_3_1", 4.0)[0] == 4
    assert cap.legal_durations("veo_3_1", 3.2)[0] == 4      # rounded up
    assert cap.legal_durations("veo_3_1", 5.0)[0] == 6
    assert cap.legal_durations("veo_3_1", 9.0)[0] is None   # beyond its ceiling


def test_clamp_respects_each_model_not_a_global_ceiling():
    """`max(3, min(10, ...))` was applied to every model regardless of its limits.

    Asserts against each model's OWN declared ceiling rather than literals. The
    previous version hardcoded 5s for wan_2_7 and 10s for seedance -- numbers I
    had guessed -- so it passed while the table was wrong and failed the moment
    the real schema was read. A test that encodes an assumption cannot catch it.
    """
    for key in cap.VIDEO_CAPS:
        ceiling = cap.spec(key)["max_seconds"]
        assert cap.clamp_duration(key, 999) == int(ceiling)
    # The global ceiling really is gone: these two genuinely differ.
    assert cap.clamp_duration("veo_3_1", 999) != cap.clamp_duration("kling_2_1_standard", 999)


def test_resolver_never_returns_an_illegal_duration():
    for seconds in (2.0, 3.0, 3.2, 4.5, 6.0, 9.9):
        r = cap.resolve({"duration": seconds})
        if not r["backend"]:
            continue
        spec = cap.spec(r["backend"])
        allowed = spec["allowed_durations"]
        got = r["generate_seconds"]
        assert spec["min_seconds"] <= got <= spec["max_seconds"]
        if allowed:
            assert got in allowed


def test_gestural_shot_is_never_given_a_model_that_must_be_trimmed():
    """Trimming a designed movement stops the head mid-turn — see director.fit_clip."""
    r = cap.resolve({"duration": 3.2, "gestural": True})
    if r["backend"]:
        assert r["generate_seconds"] <= 3.2 + 0.05


def test_impossible_request_says_so_rather_than_guessing():
    r = cap.resolve({"duration": 45.0})
    assert r["backend"] is None
    assert "no_legal_backend" in r["constraints"]


def test_character_reference_requirement_filters_models():
    r = cap.resolve({"duration": 5.0, "needs_character_reference": True})
    if r["backend"]:
        assert cap.spec(r["backend"])["supports_character_reference"]


def test_cost_estimate_includes_the_still():
    free = cap.estimate_shot_cost("parallax", 5.0, takes=1)
    paid = cap.estimate_shot_cost("ai_video", 5.0, takes=1)
    assert free == pytest.approx(cap.COST_PER_IMAGE, abs=1e-6)
    assert paid > free


# --- duration fitting: the model proposes, code decides ------------------------

@pytest.mark.parametrize("seconds,weights", [
    (26.95, [3, 2, 4, 2, 3, 2, 3]),
    (22.72, [5, 9, 3, 4]),
    (36.61, [2] * 8),
    (12.00, [1, 1]),
    (20.30, [4, 10, 3, 3]),
])
def test_durations_land_exactly_on_the_beat(seconds, weights):
    """`director.validate` rejects a plan that misses by 0.01s."""
    durs = P._fit_to_beat([{"weight": w} for w in weights], seconds)
    assert abs(sum(durs) - seconds) < 0.01
    assert min(durs) > 0


@pytest.mark.parametrize("seconds,count", [(6.0, 10), (4.0, 6), (3.0, 5), (2.0, 4), (1.0, 3)])
def test_crowded_beats_never_produce_negative_durations(seconds, count):
    """More shots than a beat can hold once drove one shot to -4.8 seconds.

    The minimum shot length pushed the total above the beat, and the whole
    remainder was subtracted from a single shot.
    """
    durs = P._fit_to_beat([{"weight": 1} for _ in range(count)], seconds)
    assert durs, "at least one shot must survive"
    assert min(durs) > 0
    assert abs(sum(durs) - seconds) < 0.01
    assert len(durs) <= count


def test_excess_shots_are_dropped_not_squeezed():
    durs = P._fit_to_beat([{"weight": 1} for _ in range(12)], 6.0)
    assert len(durs) < 12
    assert min(durs) >= P.MIN_SHOT_SECONDS - 0.001


def test_missing_weights_are_tolerated():
    durs = P._fit_to_beat([{}, {}, {}], 15.0)
    assert abs(sum(durs) - 15.0) < 0.01


def test_empty_input():
    assert P._fit_to_beat([], 10.0) == []
    assert P._fit_to_beat([{"weight": 1}], 0) == []


# --- profiles + schema ---------------------------------------------------------

def test_profile_falls_back_to_default():
    assert P.profile(None)["key"] == P.DEFAULT_PROFILE
    assert P.profile("nonsense")["key"] == P.DEFAULT_PROFILE
    assert P.profile("cinematic_documentary")["key"] == "cinematic_documentary"


def test_no_union_types_in_enumerated_properties():
    """A union type in an enum'd property is rejected by structured outputs.

    That 400 killed a whole script draft once ("Enum value 'nano2' does not match
    declared type ['string','null']"). Guard both schemas against the repeat.
    """
    def walk(node):
        if isinstance(node, dict):
            if "enum" in node and isinstance(node.get("type"), list):
                raise AssertionError(f"union type on an enum'd property: {node}")
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(P.PLAN_SCHEMA)
    walk(P.CRITIC_SCHEMA)


def test_schema_requires_every_property_it_defines():
    """Structured outputs need `required` to cover the properties, or fields vanish."""
    for schema in (P.PLAN_SCHEMA, P.CRITIC_SCHEMA):
        def walk(node):
            if isinstance(node, dict):
                if node.get("type") == "object" and "properties" in node:
                    assert set(node["properties"]) == set(node.get("required") or []), \
                        f"required does not cover properties: {sorted(node['properties'])}"
                for v in node.values():
                    walk(v)
            elif isinstance(node, list):
                for v in node:
                    walk(v)
        walk(schema)


def test_shot_vocabulary_matches_the_director_shot_dataclass():
    """The planner's enums must be values director.DirectorShot can carry."""
    from backend.director import DirectorShot
    ds = DirectorShot(id="s001.01", beat_id="s001")
    for field in ("purpose", "shot_size", "angle", "motion_type", "face_visibility"):
        assert hasattr(ds, field)
    assert set(P.MOTION_TYPES) == {"static", "parallax", "ai_video"}


# --- the locked-coverage guard -------------------------------------------------

def _sb_with(beat_ids):
    from backend.manifest import Camera, Shot, Storyboard
    return Storyboard(
        title="T",
        shots=[Shot(scene_id=b, narration="n", prompt="p",
                    camera=Camera(move="static", duration=20.0)) for b in beat_ids],
    )


def test_plan_scene_refuses_to_overwrite_covered_beats(tmp_path, monkeypatch):
    """Planning a scene must not clobber coverage that is already compiled.

    plan_scene saved a fresh status="draft" plan for every beat it was given.
    On a beat that was already compiled that discards the paid coverage record
    AND drops the beat's render protection, because has_locked_coverage accepts
    only locked/compiling/compiled -- so the next render would overwrite the
    clip it had already paid for.
    """
    from backend import director
    monkeypatch.setattr(director.config, "MANIFEST_PATH",
                        tmp_path / "storyboard_manifest.json")

    compiled = director.CoveragePlan(beat_id="s012", beat_duration=20.0,
                                     status="compiled")
    director.save_plan(compiled)

    # Every beat locked -> refuse loudly rather than silently doing nothing.
    with pytest.raises(ValueError, match="locked coverage"):
        P.plan_scene(_sb_with(["s012"]), ["s012"], log=lambda m: None)

    # The plan on disk is untouched.
    assert director.load_plan("s012").status == "compiled"
    assert director.has_locked_coverage("s012") is True


def test_replan_overrides_the_guard(tmp_path, monkeypatch):
    """The guard is a default, not a wall -- replan=True must still get through."""
    from backend import director
    monkeypatch.setattr(director.config, "MANIFEST_PATH",
                        tmp_path / "storyboard_manifest.json")
    director.save_plan(director.CoveragePlan(beat_id="s012", beat_duration=20.0,
                                             status="compiled"))
    # Gets past the guard and fails later, at the Claude call -- not at the guard.
    with pytest.raises(Exception) as exc:
        P.plan_scene(_sb_with(["s012"]), ["s012"], log=lambda m: None, replan=True)
    assert "locked coverage" not in str(exc.value)


# --- the video argument router (review round 1, finding #4) ---------------------

def test_every_model_gets_a_duration_in_its_own_spelling():
    """kling, wan and luma were sent NO duration at all, so each rendered its own
    5s default and the rest of the beat became a freeze-frame."""
    for key in cap.VIDEO_CAPS:
        args, _ = cap.video_arguments(key, 6.0, cap_to_ceiling=True)
        assert "duration" in args, f"{key} was sent no duration"
    # And in the wire type its schema declares, not a uniform string.
    assert isinstance(cap.video_arguments("wan_2_7", 6.0)[0]["duration"], int)
    assert cap.video_arguments("veo_3_1", 6.0)[0]["duration"] == "6s"
    assert cap.video_arguments("luma_dream_machine", 6.0)[0]["duration"] == "9s"


def test_seedance_is_never_sent_a_length_below_its_floor():
    """max(3, min(10, ...)) sent "3" to an enum whose minimum is 4 -> 422, and the
    beat shipped with no Tier-C clip at all."""
    args, note = cap.video_arguments("seedance_2_0", 3.2)
    assert args["duration"] == "4"
    assert "raised" in note or "rounded" in note
    allowed = {str(v) for v in cap.VIDEO_CAPS["seedance_2_0"]["duration_values"]}
    for want in (0.5, 2.0, 3.0, 3.9):
        assert str(cap.video_arguments("seedance_2_0", want)[0]["duration"]) in allowed


def test_a_long_beat_asks_for_the_maximum_not_the_default():
    """The old clamp capped everything at 10s. A 20.9s beat on seedance now asks
    for 15s -- ten more seconds of real motion, ten fewer of frozen frame."""
    args, note = cap.video_arguments("seedance_2_0", 20.86, cap_to_ceiling=True)
    assert args["duration"] == "15"
    assert "exceeds" in note and "maximum" in note


def test_director_is_told_it_cannot_be_served_rather_than_quietly_capped():
    """Coverage can re-route or split a shot; the batch render cannot. Only the
    batch render caps."""
    args, note = cap.video_arguments("seedance_2_0", 20.86, cap_to_ceiling=False)
    assert "duration" not in args
    assert "exceeds" in note


def test_generate_audio_only_where_the_schema_declares_it():
    """Decided by `elif "seedance" not in endpoint`, which was right by luck."""
    assert cap.video_arguments("seedance_2_0", 6.0)[0]["generate_audio"] is True
    assert cap.video_arguments("veo_3_1", 6.0)[0]["generate_audio"] is True
    for key in ("kling_2_1_standard", "kling_2_master", "wan_2_7", "luma_dream_machine"):
        assert "generate_audio" not in cap.video_arguments(key, 6.0)[0], key


def test_veo_rounds_the_same_direction_as_the_planner():
    """The batch path rounded 5.0s DOWN to "4s" while legal_durations rounds it up,
    so one shot came out a different length depending on which path rendered it."""
    assert cap.video_arguments("veo_3_1", 5.0)[0]["duration"] == "6s"
    assert cap.legal_durations("veo_3_1", 5.0)[0] == 6


def test_aliased_model_keys_resolve_to_real_capabilities():
    """main.py resolved the alias when building the endpoint URL but passed the RAW
    key to the capability lookup, so the request went to one model while the limits
    came from the permissive CONTINUOUS fallback (min 3, max 10, no enum)."""
    from backend import assets
    for alias, real in list(assets.VIDEO_BACKEND_ALIASES.items())[:8]:
        if real not in cap.VIDEO_CAPS:
            continue
        assert cap.spec(alias)["key"] == real, alias
        assert cap.spec(alias)["allowed_durations"] == cap.VIDEO_CAPS[real]["allowed_durations"]
    # kling_2_5_turbo_pro is the entry that rendered on a different model for
    # months; it must now report Kling 2.1's real 5/10 enum, not 3-10 continuous.
    sp = cap.spec("kling_2_5_turbo_pro")
    assert sp["allowed_durations"] == [5.0, 10.0]
    assert cap.video_arguments("kling_2_5_turbo_pro", 3.0)[0]["duration"] == "5"


def test_an_unknown_key_still_falls_back_rather_than_raising():
    sp = cap.spec("something_nobody_registered")
    assert sp["key"] == "something_nobody_registered"
    assert sp["max_seconds"] > 0
