"""The six-stage spine: gating, blocking reasons, and the narrator setting.

These are behavioral tests. They assert what a user is allowed to do next and
what the server refuses to claim, not how the computation is arranged — the
point of moving stage gating server-side is that only one place decides it, so
the tests describe the decisions rather than the plumbing.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend import stages  # noqa: E402
from backend.manifest import Camera, Shot, Storyboard, narrator_name  # noqa: E402


def _paths(tmp_path: Path) -> dict:
    for k in ("render", "narration", "sfx"):
        (tmp_path / k).mkdir(exist_ok=True)
    return {
        "slug": "ep",
        "render": tmp_path / "render",
        "narration": tmp_path / "narration",
        "sfx": tmp_path / "sfx",
    }


def _sb(n: int = 3, **kw) -> Storyboard:
    return Storyboard(
        title="Test Episode",
        shots=[Shot(scene_id=f"s{i:03d}", narration="x", camera=Camera(duration=6.0))
               for i in range(1, n + 1)],
        **kw,
    )


def _by_id(payload: dict) -> dict:
    return {s["id"]: s for s in payload["stages"]}


def _render(paths: dict, *scene_ids: str) -> None:
    for sid in scene_ids:
        (paths["render"] / f"{sid}.mp4").write_bytes(b"x")


# --- the spine itself -----------------------------------------------------------

def test_spine_is_the_six_contract_stages_in_order(tmp_path):
    payload = stages.payload(_sb(), _paths(tmp_path))
    assert [s["id"] for s in payload["stages"]] == [
        "script", "direct", "generate", "roughcut", "refine", "export",
    ]


def test_every_stage_carries_a_primary_cta(tmp_path):
    """Contract §2.1 fixes one primary action per stage; none may be missing."""
    payload = stages.payload(_sb(), _paths(tmp_path))
    assert all(s["cta"] and s["cta_action"] for s in payload["stages"])
    assert _by_id(payload)["generate"]["cta"] == "Build Draft 1"


def test_exactly_one_stage_is_current(tmp_path):
    payload = stages.payload(_sb(), _paths(tmp_path))
    assert sum(1 for s in payload["stages"] if s["status"] == "current") == 1


def test_next_action_names_the_current_stage(tmp_path):
    payload = stages.payload(_sb(), _paths(tmp_path))
    current = next(s for s in payload["stages"] if s["status"] == "current")
    assert payload["next_action"]["stage"] == current["id"]
    assert payload["next_action"]["label"] == current["cta"]


# --- gating ---------------------------------------------------------------------

def test_direct_is_blocked_until_the_script_locks(tmp_path):
    paths = _paths(tmp_path)
    blocked = _by_id(stages.payload(_sb(), paths))["direct"]
    assert blocked["status"] == "blocked"
    assert "not locked" in blocked["blocked_reason"]

    unblocked = _by_id(stages.payload(_sb(script_locked=True), paths))["direct"]
    assert unblocked["status"] != "blocked"


def test_generate_is_blocked_until_the_storyboard_is_approved(tmp_path):
    """Gate 1. CLAUDE.md makes the paid tier unreachable before approval."""
    paths = _paths(tmp_path)
    gen = _by_id(stages.payload(_sb(script_locked=True), paths))["generate"]
    assert gen["status"] == "blocked"
    assert "not approved" in gen["blocked_reason"]

    approved = _sb(script_locked=True, storyboard_approved=True)
    assert _by_id(stages.payload(approved, paths))["generate"]["status"] != "blocked"


def test_a_locked_script_alone_does_not_unblock_generate(tmp_path):
    """Locking the script must not be mistaken for allocating the budget."""
    gen = _by_id(stages.payload(_sb(script_locked=True), _paths(tmp_path)))["generate"]
    assert gen["status"] == "blocked"


def test_rough_cut_opens_on_partial_coverage(tmp_path):
    """§6.2: Draft 1 may be built before generation completes, with placeholders."""
    paths = _paths(tmp_path)
    sb = _sb(3, script_locked=True, storyboard_approved=True)
    _render(paths, "s001")

    rough = _by_id(stages.payload(sb, paths))["roughcut"]
    assert rough["status"] != "blocked"
    assert "2 placeholders" in rough["hint"]


def test_rough_cut_is_blocked_with_no_visuals_at_all(tmp_path):
    sb = _sb(script_locked=True, storyboard_approved=True)
    rough = _by_id(stages.payload(sb, _paths(tmp_path)))["roughcut"]
    assert rough["status"] == "blocked"


def test_refine_and_export_wait_for_a_draft(tmp_path):
    paths = _paths(tmp_path)
    sb = _sb(1, script_locked=True, storyboard_approved=True)
    _render(paths, "s001")

    stages_by_id = _by_id(stages.payload(sb, paths))
    assert stages_by_id["refine"]["status"] == "blocked"
    assert stages_by_id["export"]["status"] == "blocked"

    (paths["render"] / "_preview.mp4").write_bytes(b"x")
    with_draft = _by_id(stages.payload(sb, paths))
    assert with_draft["refine"]["status"] != "blocked"
    assert with_draft["export"]["status"] != "blocked"


# --- no false success (§11.4) ---------------------------------------------------

def test_generate_is_not_complete_while_a_shot_is_missing(tmp_path):
    paths = _paths(tmp_path)
    sb = _sb(3, script_locked=True, storyboard_approved=True)
    _render(paths, "s001", "s002")
    assert _by_id(stages.payload(sb, paths))["generate"]["status"] != "complete"

    _render(paths, "s003")
    assert _by_id(stages.payload(sb, paths))["generate"]["status"] == "complete"


def test_the_preview_is_not_counted_as_a_rendered_beat(tmp_path):
    """_preview.mp4 lives in the render dir; counting it would fake coverage."""
    paths = _paths(tmp_path)
    sb = _sb(2, script_locked=True, storyboard_approved=True)
    (paths["render"] / "_preview.mp4").write_bytes(b"x")

    counts = stages.count(sb, paths)
    assert counts.rendered == 0
    assert counts.has_draft is True


def test_refine_never_claims_completion(tmp_path):
    """§8: optional polish must not block export, so it cannot assert a review."""
    paths = _paths(tmp_path)
    sb = _sb(1, script_locked=True, storyboard_approved=True)
    _render(paths, "s001")
    (paths["render"] / "_preview.mp4").write_bytes(b"x")
    assert _by_id(stages.payload(sb, paths))["refine"]["status"] != "complete"


def test_export_is_complete_only_when_a_deliverable_exists(tmp_path):
    paths = _paths(tmp_path)
    sb = _sb(1, script_locked=True, storyboard_approved=True)
    _render(paths, "s001")
    (paths["render"] / "_preview.mp4").write_bytes(b"x")
    assert _by_id(stages.payload(sb, paths))["export"]["status"] != "complete"

    (tmp_path / "ep.fcpxml").write_text("<xml/>", encoding="utf-8")
    done = _by_id(stages.payload(sb, paths))["export"]
    assert done["status"] == "complete"
    assert "fcpxml" in done["hint"]


# --- coverage counting ----------------------------------------------------------

def test_locked_coverage_is_counted_from_plan_status(tmp_path):
    sb = _sb(3, script_locked=True)
    statuses = {"s001": "locked", "s002": "draft", "s003": None}
    counts = stages.count(sb, _paths(tmp_path), lambda bid: statuses.get(bid))
    assert counts.planned == 2
    assert counts.locked == 1


def test_stage_computation_reads_no_process_globals():
    """Slice 1 depends on this: stages resolve from arguments, never a global.

    Parsed rather than grepped -- the prose in this module discusses the globals
    it avoids, and a substring check cannot tell an explanation from a use.
    """
    import ast, inspect
    tree = ast.parse(inspect.getsource(stages))
    names = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    names |= {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    assert "MANIFEST_PATH" not in names
    assert "get_current_project" not in names


# --- narrator (C6) --------------------------------------------------------------

def test_narrator_defaults_to_vesper_for_existing_projects():
    assert narrator_name(Storyboard(title="t")) == "Vesper"


def test_narrator_is_overridable_per_project():
    assert narrator_name(Storyboard(title="t", narrator_name="Corvid")) == "Corvid"


def test_blank_narrator_falls_back_rather_than_rendering_empty():
    assert narrator_name(Storyboard(title="t", narrator_name="   ")) == "Vesper"


def test_narrator_survives_a_manifest_round_trip():
    sb = Storyboard(title="t", narrator_name="Corvid")
    revived = Storyboard.from_dict("p", sb.to_dict(), sb.to_dict()["shots"])
    assert narrator_name(revived) == "Corvid"


@pytest.mark.parametrize("module", ["stages"])
def test_no_hardcoded_narrator_in_new_filmcraft_code(module):
    """Contract §4: FilmCraft code must not name a narrator."""
    import importlib, inspect
    assert "Vesper" not in inspect.getsource(importlib.import_module(f"backend.{module}"))
