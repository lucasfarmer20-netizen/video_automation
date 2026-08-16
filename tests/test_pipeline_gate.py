"""Gate 1 as ``pipeline.py`` enforces it — the orchestrator's own re-read.

``Storyboard.gate_cleared()`` is pinned term by term in tests/test_manifest.py,
and the studio's paid routes are pinned in tests/test_gates_and_writes.py and
tests/test_director.py. ``pipeline.stage_gate`` is the fourth door and had no
test at all: mutating either of its two branches away left the whole suite
green. CLAUDE.md names pipeline.py as the only entrypoint and Gate 1 as
non-bypassable, so "the CLI advances into the render stages on an unapproved
storyboard" is exactly the failure the gate exists to prevent, and nothing was
watching for it.

What is being pinned is the decision, not the prose:

  * a gate that is not clear must stop the run — ``stage_gate`` returns ``None``
    and the caller (``run_pipeline``) returns rather than rendering;
  * the verdict comes from the manifest on disk, not from the object the caller
    happens to be holding. The whole point of the pause is that a human changed
    something in the studio while the process waited, so trusting the in-memory
    storyboard would make the reload decorative;
  * a gate that IS clear must not stop the run, because a gate that fails closed
    on approved work trains people to route around it.

Assertion order is deliberate. The verdict is asserted first in every test here;
the console text second. A test that checked the message first would go red on
the wording under a mutation that opened the gate, and the table would be red
having demonstrated nothing about whether the run was stopped.

Nothing here touches the repository's own manifest: ``config.MANIFEST_PATH`` is
monkeypatched to tmp_path, which is what ``manifest.load()`` resolves through
when no project context is bound.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
for _m in ("anthropic", "fal_client", "elevenlabs"):
    sys.modules.setdefault(_m, types.ModuleType(_m))

import pipeline  # noqa: E402
from backend import config, manifest  # noqa: E402
from backend.manifest import MotionType, Shot, Storyboard  # noqa: E402


def _paid(scene_id: str, *, approved: bool = True,
          video_model: str | None = "seedance_2_0") -> Shot:
    return Shot(scene_id=scene_id, motion_type=MotionType.AI_VIDEO,
                approved=approved, video_model=video_model)


@pytest.fixture
def manifest_on_disk(tmp_path, monkeypatch):
    """Point the process at a manifest inside tmp_path and return a writer."""
    path = tmp_path / "storyboard_manifest.json"
    monkeypatch.setattr(config, "MANIFEST_PATH", path)

    def write(sb: Storyboard) -> Storyboard:
        manifest.save(sb, path)
        return sb

    return write


def test_an_uncleared_gate_stops_the_pipeline(manifest_on_disk, capsys):
    """The run must not advance. `None` is how stage_gate says so — run_pipeline
    returns on it, and every render stage is downstream of that return."""
    sb = manifest_on_disk(Storyboard(
        title="T", storyboard_approved=False,
        shots=[_paid("s001", approved=False, video_model=None)]))

    assert pipeline.stage_gate(sb, "127.0.0.1", 5000) is None

    out = capsys.readouterr().out
    assert "HUMAN-APPROVAL GATE" in out, "the human must be told where to go"
    assert "Gate NOT cleared" in out, "and told the gate is still shut"


def test_a_storyboard_approved_with_an_unready_paid_beat_still_stops_it(
        manifest_on_disk, capsys):
    """Approving the storyboard is not approving the spend.

    This is the case the flag alone cannot catch, and the reason stage_gate asks
    gate_cleared() rather than reading storyboard_approved: the episode is
    approved and one Tier-C beat has no model to render on."""
    sb = manifest_on_disk(Storyboard(
        title="T", storyboard_approved=True,
        shots=[_paid("s001"), _paid("s002", video_model=None)]))

    assert pipeline.stage_gate(sb, "127.0.0.1", 5000) is None

    assert "Gate NOT cleared" in capsys.readouterr().out


def test_a_cleared_gate_lets_the_pipeline_through(manifest_on_disk, capsys):
    """The other direction. A gate that refuses approved work is not a safer
    gate, it is a gate people learn to route around."""
    sb = manifest_on_disk(Storyboard(title="T", storyboard_approved=True,
                                     shots=[_paid("s001")]))

    assert pipeline.stage_gate(sb, "127.0.0.1", 5000) is sb

    assert "Gate already cleared" in capsys.readouterr().out


def test_the_verdict_comes_from_the_manifest_not_the_object_in_hand(
        manifest_on_disk, capsys):
    """The pause exists so a human can approve in the studio while the process
    waits, so the reload is the whole mechanism. An in-memory verdict would make
    the run un-resumable: the caller's storyboard is by construction the
    pre-approval one."""
    approved = Storyboard(title="T", storyboard_approved=True,
                          shots=[_paid("s001")])
    manifest_on_disk(approved)
    stale = Storyboard(title="T", storyboard_approved=False,
                       shots=[_paid("s001", approved=False)])

    cleared = pipeline.stage_gate(stale, "127.0.0.1", 5000)

    assert cleared is not None, "the approval written while we waited must count"
    assert cleared.storyboard_approved is True
    assert cleared is not stale, "the returned board must be the reloaded one"
    assert "Gate cleared" in capsys.readouterr().out
