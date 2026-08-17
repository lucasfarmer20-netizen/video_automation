"""Mutation harness for the cost-quote fix.

Each mutation is a faithful, plausible way to get this wrong -- the shape a
future edit would actually take, not a scrambled character. A mutation that
survives means the tests certify a fix they do not exercise.

Run:  python scratch/mutate_cost_quote.py
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# Both files, deliberately. test_cost_quote_matches_ledger proves the quote and
# the ledger agree; test_fal_tariff proves the number they agree ON is fal's.
# Round 3 ran only the first, and a reviewer's mutation -- billing one second
# regardless of duration -- survived all twelve of its tests, because two figures
# agreeing is not evidence when nothing anchors either to the authority.
TESTS = ["tests/test_cost_quote_matches_ledger.py", "tests/test_fal_tariff.py"]

# (label, file, find, replace, what breaking it would mean)
MUTATIONS = [
    # --- the price is fal's, not ours (round 4) ---------------------------------
    (
        "clip_price bills one second regardless of duration",
        "backend/capabilities.py",
        "    return round(float(generate_seconds) * float(spec(key)[\"cost_per_second\"]), 4)",
        "    return round(float(spec(key)[\"cost_per_second\"]), 4)",
        "THE REVIEWER'S MUTATION. Survived all 12 tests in round 3: quote and "
        "ledger still agreed, on a number neither had checked against fal",
    ),
    (
        "seedance reverts to the 0.10/s that was 3x under fal",
        "backend/capabilities.py",
        '        "cost_per_second": 0.3024,\n        "price_basis": "standard tier, 720p',
        '        "cost_per_second": 0.10,\n        "price_basis": "standard tier, 720p',
        "the DEFAULT backend quotes a third of what fal bills",
    ),
    (
        "veo reverts to the with-audio rate on a path that sends no audio",
        "backend/capabilities.py",
        '        "cost_per_second": 0.20,\n        "price_basis": "720p/1080p WITHOUT audio',
        '        "cost_per_second": 0.40,\n        "price_basis": "720p/1080p WITHOUT audio',
        "a quote at double the published silent rate",
    ),
    (
        "kling's base-plus-increment is flattened to the wrong per-second",
        "backend/capabilities.py",
        '        "cost_per_second": 0.056,',
        '        "cost_per_second": 0.05,',
        "$0.25 for a 5s clip fal prices at $0.28",
    ),
    (
        "an unpriced model falls back to a cheap guess",
        "backend/capabilities.py",
        '    "cost_per_second": 0.3024,\n    "price_basis": "unknown model',
        '    "cost_per_second": 0.12,\n    "price_basis": "unknown model',
        "the model nobody has priced becomes the one the router prefers",
    ),
    (
        "our own estimate is booked as the provider's bill again",
        "backend/director.py",
        "                            generation.succeed(plan.beat_id, att.id, ds.clip)",
        "                            generation.succeed(plan.beat_id, att.id, ds.clip,\n"
        "                                               cost=clip_price)",
        "every attempt reads as a confirmed invoice; cost and estimated_cost "
        "stop meaning different things",
    ),
    # --- the quote and the ledger are one function (round 3) --------------------
    (
        "the charge is a constant again, independent of the quote",
        "backend/director.py",
        "                            generation.succeed(plan.beat_id, att.id, ds.clip)",
        "                            generation.succeed(plan.beat_id, att.id, ds.clip,\n"
        "                                               cost=0.60)",
        "two prices again; they agree today only by coincidence",
    ),
    (
        "the pre-dispatch estimate is a constant again",
        "backend/director.py",
        "                            estimated_cost=clip_price,",
        "                            estimated_cost=0.60,",
        "after a crash the at-risk figure is not what was quoted",
    ),
    (
        "identity_critical stills are quoted as one image",
        "backend/director.py",
        "    return 4 if ds.identity_critical else 1",
        "    return 1",
        "four stills bought, one quoted",
    ),
    (
        "the compile buys stills without opening an attempt",
        "backend/director.py",
        """                    att_img, how_img = generation.begin(
                        beat_id=plan.beat_id, shot_id=ds.id, signature="",
                        kind="image", backend=img_backend, paid=True,
                        estimated_cost=still_price)""",
        """                    att_img, how_img = (
                        type("A", (), {"id": "", "attempt": 0})(), "created")""",
        "still spend invisible to the ledger, as before",
    ),
    (
        "the compile folds its spend back into the quote",
        "backend/director.py",
        "                            ds.clip = config.rel_media_path(target)\n"
        "                            ds.paid_clip = ds.clip",
        "                            ds.estimated_cost += clip_price\n"
        "                            ds.clip = config.rel_media_path(target)\n"
        "                            ds.paid_clip = ds.clip",
        "the post-compile doubling, re-introduced",
    ),
    (
        "estimated_cost is trusted from the file instead of derived",
        "backend/director.py",
        """    for ds in plan.coverage:
        fresh = quote_shot(ds)
        if abs(fresh - float(ds.estimated_cost or 0.0)) >= 0.005:
            ds.estimated_cost = fresh
            changed = True""",
        "    pass",
        "plans already carrying the doubled figure keep quoting it",
    ),
    (
        "an unquotable paid shot opens an attempt at $0.00",
        "backend/director.py",
        """                        clip_price = paid_clip_price(ds)
                        if clip_price is None:""",
        """                        clip_price = paid_clip_price(ds) or 0.0
                        if False:""",
        "a confident zero on money that may yet go",
    ),
    (
        "scene_summary reports spend from nowhere",
        "backend/planner.py",
        "        b_spend = _beat_spend(bid)",
        "        b_spend = {'spent': 0.0, 'at_risk': 0.0, 'at_risk_attempts': 0,\n"
        "                   'attempts': 0, 'failed': 0, 'paid_attempts': 0,\n"
        "                   'spend_is_certain': True, 'summary': ''}",
        "a compiled beat reports $0.00 spent",
    ),
    (
        "the planner prices shots inline again",
        "backend/planner.py",
        "            shot.estimated_cost = director.quote_shot(shot)",
        "            shot.estimated_cost = round(capabilities.COST_PER_IMAGE, 3)",
        "the quote stops tracking the charge at the source",
    ),
]


def run(work: Path) -> tuple[int, str]:
    p = subprocess.run(
        [sys.executable, "-m", "pytest", *TESTS, "-x", "-q", "-p", "no:cacheprovider"],
        cwd=work, capture_output=True, text=True)
    return p.returncode, (p.stdout + p.stderr)[-1500:]


def main() -> int:
    survivors = []
    for label, rel, find, repl, why in MUTATIONS:
        with tempfile.TemporaryDirectory() as td:
            work = Path(td) / "repo"
            shutil.copytree(ROOT, work, ignore=shutil.ignore_patterns(
                ".git", "__pycache__", "node_modules", ".pytest_cache", "*.mp4",
                "*.png", "*.wav", "*.mp3", "render", "scratch"))
            target = work / rel
            src = target.read_text(encoding="utf-8")
            if find not in src:
                print(f"[ SKIP ] {label}\n         anchor not found in {rel}")
                survivors.append((label, "anchor not found — harness is stale"))
                continue
            target.write_text(src.replace(find, repl, 1), encoding="utf-8")
            code, tail = run(work)
            if code == 0:
                print(f"[SURVIVES] {label}")
                print(f"           would mean: {why}")
                survivors.append((label, why))
            else:
                print(f"[ KILLED ] {label}")

    print()
    if survivors:
        print(f"{len(survivors)} mutation(s) survived:")
        for label, why in survivors:
            print(f"  - {label}: {why}")
        return 1
    print(f"all {len(MUTATIONS)} mutations killed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
