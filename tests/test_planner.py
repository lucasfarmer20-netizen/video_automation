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
    """`max(3, min(10, ...))` was applied to every model regardless of its limits."""
    assert cap.clamp_duration("wan_2_7", 30) == 5           # real max is 5s
    assert cap.clamp_duration("veo_3_1", 30) == 8
    assert cap.clamp_duration("seedance_2_0", 30) == 10


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
    assert free == pytest.approx(cap.COST_PER_IMAGE)
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
