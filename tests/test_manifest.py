"""Unit tests for backend/manifest.py: the Gate 1 predicate, and load/save.

`Storyboard.gate_cleared()` is the whole of Gate 1 in code. pipeline.py branches
on it twice (pipeline.py:174, pipeline.py:188) and main.py reports it to the
studio (main.py:3847), and until now nothing exercised it directly — every
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


# --- Gate 1: Storyboard.gate_cleared() ----------------------------------------

def test_gate_shut_when_the_storyboard_is_not_approved():
    """The human approval flag is checked first and alone. Every shot below is
    otherwise ready, so if this passes it is that flag doing the work."""
    sb = Storyboard(title="T", storyboard_approved=False,
                    shots=[_paid("s001"), _free("s002")])
    assert sb.gate_cleared() is False


def test_gate_shut_when_a_paid_shot_is_unapproved():
    """Approving the storyboard is not approving the spend. Each paid beat is
    its own line item and needs its own tick."""
    sb = Storyboard(title="T", storyboard_approved=True,
                    shots=[_paid("s001"), _paid("s002", approved=False)])
    assert sb.gate_cleared() is False


@pytest.mark.parametrize("video_model", [None, ""])
def test_gate_shut_when_a_paid_shot_has_no_video_model(video_model):
    """`bool(video_model)` — so an unset model and a blank string both hold the
    gate. Approving a Tier-C beat with nothing to render it on would send the
    pipeline at a paid API with no model selected."""
    sb = Storyboard(title="T", storyboard_approved=True,
                    shots=[_paid("s001", video_model=video_model)])
    assert sb.gate_cleared() is False


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
    """main.py:1083 and main.py:3149 report len(paid_shots()) as the spend
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
    it. A shot left holding the raw string "ai_video" compares unequal to
    MotionType.AI_VIDEO under `==` on an Enum member, so needs_paid_video()
    would return False and the beat would slip past the budget gate."""
    sb = Storyboard(title="T", shots=[_paid("s001"),
                                      _free("s002", MotionType.STATIC)])
    assert sb.to_dict()["shots"][0]["motion_type"] == "ai_video"

    back = _roundtrip(sb)
    assert back.shots[0].motion_type is MotionType.AI_VIDEO
    assert back.shots[1].motion_type is MotionType.STATIC
    assert back.shots[0].needs_paid_video() is True


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


def test_an_unrecognised_motion_type_raises():
    """Documenting current behaviour, not endorsing it: unknown *keys* are
    filtered out, but an unknown motion_type *value* propagates a ValueError out
    of from_dict. That lands in the same get_current_project fallback the key
    filtering exists to avoid. Flagged for review; deliberately not changed here,
    since a fallback would be a behaviour change and this suite is tests-only."""
    with pytest.raises(ValueError):
        Storyboard.from_dict("p", {}, [{"scene_id": "s001",
                                        "motion_type": "claymation"}])


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
