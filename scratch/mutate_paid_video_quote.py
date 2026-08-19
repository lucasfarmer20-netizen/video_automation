"""Mutation harness for the beat-level paid-video cost gate.

Each mutation is a faithful, plausible way to get this wrong -- the shape a
future edit would actually take, and in several cases the literal shape the code
had before this change. A mutation that survives means the tests certify a fix
they do not exercise.

Run:  python scratch/mutate_paid_video_quote.py
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# Three files, deliberately. The first proves the dispatch is gated and recorded;
# the second anchors the numbers to fal, so a mutation that makes the gate agree
# with a wrong price cannot pass by internal consistency; the third is the
# compile path's own quote, which must not move.
TESTS = [
    "tests/test_beat_paid_video_quote.py",
    "tests/test_fal_tariff.py",
    "tests/test_cost_quote_matches_ledger.py",
]

# (label, file, find, replace, what breaking it would mean)
MUTATIONS = [
    # --- the gate itself, reverted ------------------------------------------------
    (
        "the route dispatches whatever it is sent (the original code)",
        "backend/main.py",
        "        if not paid_video.accepted_matches(accepted, quote[\"estimated_cost\"]):",
        "        if False:",
        "THE DEFECT. Paid video bought with no quote and no confirmation",
    ),
    (
        "the batch loop stops spending the authorisation (the original code)",
        "backend/main.py",
        "                    authorised.spend(q, f\"{shot.scene_id}\")",
        "                    pass",
        "a whole-episode render buys a clip per Tier-C beat against nothing",
    ),
    (
        "an unauthorised render is allowed rather than refused",
        "backend/main.py",
        "    authorised = authorised or paid_video.Authorisation.none(",
        "    authorised = authorised or paid_video.Authorisation.accepting(1e9) or paid_video.Authorisation.none(",
        "the default becomes a bypass, so any caller that forgets the argument "
        "spends freely -- the exact failure mode of gating routes not spends",
    ),
    (
        "Authorisation.none() authorises everything",
        "backend/paid_video.py",
        "    def none(cls, why: str = \"no price was quoted or confirmed for this request\"):\n        return cls(None, why)",
        "    def none(cls, why: str = \"no price was quoted or confirmed for this request\"):\n        return cls(float(\"inf\"), why)",
        "the absence of consent becomes unlimited consent",
    ),
    (
        "the route's gate checks truthiness instead of the number",
        "backend/paid_video.py",
        "    if not is_a_number(accepted):\n        return False\n    return abs(float(accepted) - float(quoted)) < VISIBLE",
        "    return bool(accepted)",
        "GATE 1'S OWN DEFECT IN A NEW PLACE: a truthy non-number clears the "
        "cost gate, so {\"accepted_cost\": true} buys a $3.20 clip",
    ),
    (
        "the gate accepts anything at or above the quote",
        "backend/paid_video.py",
        "    return abs(float(accepted) - float(quoted)) < VISIBLE",
        "    return float(accepted) >= float(quoted)",
        "a figure computed from a different state is accepted as consent",
    ),
    (
        "is_a_number stops asking the type first",
        "backend/paid_video.py",
        "    if isinstance(value, bool) or not isinstance(value, (int, float)):\n        return False\n    return math.isfinite(float(value))",
        "    try:\n        return math.isfinite(float(value))\n    except (TypeError, ValueError):\n        return False",
        "any object with __float__ -- or a string of the right digits -- passes "
        "as a confirmed price",
    ),

    # --- the price is the price of the request actually being made ----------------
    (
        "clip_price ignores audio and always quotes the silent rate",
        "backend/capabilities.py",
        "    rate = rate_per_second(key, generate_audio=generate_audio)",
        "    rate = rate_per_second(key, generate_audio=False)",
        "THE ORIGINAL UNDERSTATEMENT: veo with audio quoted at half what fal "
        "bills, on a path that passes the toggle straight through",
    ),
    (
        "the audio rate is read only where it is convenient",
        "backend/capabilities.py",
        '        "cost_per_second_audio": 0.40,',
        '        "cost_per_second_audio": 0.20,',
        "veo's with-audio rate reverts to the silent one",
    ),
    (
        "clip_price defaults audio ON",
        "backend/capabilities.py",
        "def clip_price(key: str, generate_seconds: float, *,\n               generate_audio: bool = False) -> float:",
        "def clip_price(key: str, generate_seconds: float, *,\n               generate_audio: bool = True) -> float:",
        "the new argument moves a number the compile path has already quoted "
        "humans -- the one thing extending this function must not do",
    ),
    (
        "the quote prices the beat's length rather than the length requested",
        "backend/paid_video.py",
        "    seconds = capabilities.clamp_duration(key, float(target_seconds))",
        "    seconds = float(target_seconds)",
        "a 3.34s beat billed at kling's 5s minimum is quoted for 3.34s",
    ),

    # --- recording (contract 6.1) -------------------------------------------------
    (
        "the paid clip is dispatched without opening an attempt",
        "backend/main.py",
        "        video_rel_path = record_paid_video(scene_id, priced, buy_the_clip)",
        "        video_rel_path = buy_the_clip()",
        "money spent, no record -- the charge is invisible to spend() and "
        "cannot be reconciled against a fal invoice",
    ),
    (
        "the attempt is opened with no cost on it",
        "backend/main.py",
        "                                estimated_cost=q.price)",
        "                                estimated_cost=0.0)",
        "a paid generation reports $0.00, which is how the ledger came to "
        "understate the paid tier in the first place",
    ),
    (
        "the ledger records a figure of its own rather than the quote",
        "backend/main.py",
        "        priced = paid_video_quote(sb, shot, video_model_key)\n        authorised.spend(priced, scene_id)",
        "        priced = paid_video.quote(video_model_key, 5.0, generate_audio=False)\n        authorised.spend(priced, scene_id)",
        "consent taken for one number and another recorded -- the exact defect "
        "clip_price was collapsed into one function to remove",
    ),

    # --- the batch route ----------------------------------------------------------
    (
        "the render route stops requiring a confirmed total",
        "backend/main.py",
        "            authorised, refusal = authorise_batch_render(sb, force_paid, accepted_cost)\n            if refusal is not None:\n                return refusal",
        "            authorised = paid_video.Authorisation.accepting(1e9)",
        "the largest paid action in the studio runs on no number at all",
    ),
    (
        "a render quoted as free authorises anything anyway",
        "backend/main.py",
        "        return paid_video.Authorisation.none(\n            \"this render was quoted as buying no paid video\"), None",
        "        return paid_video.Authorisation.accepting(1e9), None",
        "a beat the estimate missed is bought unquoted -- precisely the case "
        "where the human was shown nothing for it",
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
