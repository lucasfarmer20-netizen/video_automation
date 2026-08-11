"""A partial generation run must leave evidence behind.

MichaelHeney drafted 19 of 25 beats with one take instead of three. Nothing
raised at the batch level, every beat had drafts, and the ledger recorded 78
successes and zero failures -- so after the container restarted there was no way
left to answer which strategy or endpoint had been rejecting the calls.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.modules.setdefault("anthropic", types.ModuleType("anthropic"))
sys.modules.setdefault("fal_client", types.ModuleType("fal_client"))

from backend import ledger  # noqa: E402


@pytest.fixture
def led(tmp_path, monkeypatch):
    monkeypatch.setenv("PROMPT_LEDGER_PATH", str(tmp_path / "prompt_ledger.jsonl"))
    return tmp_path / "prompt_ledger.jsonl"


def test_failure_is_recorded_with_enough_to_attribute_it(led):
    ledger.record_failure(scene_id="s005", strategy="chiaroscuro",
                          backend="flux_2_pro", batch="1785992288", slot=1,
                          error="ContentPolicyError: rejected", prompt="a mountain grade")
    rows = ledger.failures()
    assert len(rows) == 1
    r = rows[0]
    # Each of these is needed to answer a different question about the shortfall:
    # which beat, which strategy, which endpoint, which slot went missing, why.
    assert (r["scene_id"], r["strategy"], r["backend"], r["slot"]) == \
           ("s005", "chiaroscuro", "flux_2_pro", 1)
    assert "ContentPolicyError" in r["error"]
    assert r["event"] == "generate_failed"


def test_failures_do_not_pollute_the_success_summary(led):
    """summary() counts generations; a failure must not read as one."""
    ledger.record_generation(scene_id="s005", path="assets/s005/var_1_0.png",
                             strategy="baseline", prompt="p", backend="flux_2_pro",
                             batch="b1", slot=0)
    ledger.record_failure(scene_id="s005", strategy="chiaroscuro",
                          backend="flux_2_pro", batch="b1", slot=1, error="boom")
    assert ledger.summary()["generated"] == 1
    assert len(ledger.failures()) == 1


def test_failures_are_filterable_to_one_run(led):
    for batch in ("b1", "b1", "b2"):
        ledger.record_failure(scene_id="s005", strategy="baseline",
                              backend="flux_2_pro", batch=batch, slot=0, error="x")
    assert len(ledger.failures(batch="b1")) == 2
    assert len(ledger.failures(batch="b2")) == 1
    assert len(ledger.failures()) == 3


def test_a_long_error_is_truncated_not_dropped(led):
    ledger.record_failure(scene_id="s005", strategy="baseline", backend="b",
                          batch="b1", slot=0, error="E" * 5000)
    err = ledger.failures()[0]["error"]
    assert 0 < len(err) <= 600


def test_generate_for_shot_records_every_swallowed_variant(led, tmp_path, monkeypatch):
    """The wiring, not just the ledger call.

    The per-variant handler catches and continues so one bad strategy cannot cost
    a beat its other takes -- correct, but on its own it made the shortfall
    invisible the moment the container recycled.
    """
    from backend import assets
    from backend.manifest import Camera, Shot

    monkeypatch.setattr(assets, "PROMPT_VARIANTS", True)
    monkeypatch.setattr(assets.config, "ASSETS", tmp_path / "assets")

    def boom(*a, **kw):
        raise RuntimeError("rejected by the content filter")

    monkeypatch.setattr(assets, "_generate_urls", boom)

    shot = Shot(scene_id="s005", narration="n", prompt="a mountain railway grade",
                camera=Camera(move="static", duration=6.0))
    logged: list[str] = []
    paths = assets.generate_for_shot(shot, n=3, backend="flux_2_pro",
                                     log=logged.append)

    assert paths == []                       # nothing generated, nothing claimed
    rows = ledger.failures()
    assert len(rows) == 3, "every failed take needs its own row"
    assert {r["slot"] for r in rows} == {0, 1, 2}
    assert all(r["backend"] == "flux_2_pro" for r in rows)
    assert len({r["strategy"] for r in rows}) == 3, "each take used a distinct strategy"
    assert all("content filter" in r["error"] for r in rows)
    # And it is stated in the log, not only buried in the ledger.
    assert any("0 of 3 takes" in m for m in logged), logged
