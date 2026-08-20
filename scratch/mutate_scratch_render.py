"""Mutation harness for the local-render / publish-once change.

Guardrails require a test that fails under a faithful mutation of the fix, and a
mutation that genuinely reproduces the original defect. Both directions are run
here: mutated (must FAIL) and restored (must PASS).

The original defect is the easy one to reproduce faithfully, because the fix is
a redirection: putting the destination path back where the local path now goes
IS the old code. Mutation 1 does exactly that to the shared helper, and
mutations 2 and 3 do it at the two call sites the Cloud Run logs named --
`motion.render_shot` (the observed ffmpeg command) and `director.concat` (the
object that took 82 rate-limited retries).

The rest weaken a specific guarantee rather than reverting the redirection:
losing the render on a failed copy (4), publishing non-atomically where rename
works (5), staging under a servable name (6), and dropping the retry (7).

Every mutated file is snapshotted before and hash-verified after, because a
green suite is not evidence the tree is clean.

    python scratch/mutate_scratch_render.py
"""

from __future__ import annotations

import hashlib
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SUITE = ["tests/test_scratch_render.py"]

Edit = tuple[str, str, str]          # (relative path, find, replace)


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


MUTATIONS: list[tuple[str, str, list[Edit]]] = [
    (
        "helper hands the encoder the destination itself",
        "Reproduces the original defect exactly: `staged` yields `dest`, so every "
        "muxer flush lands on the bucket. This IS the pre-change behaviour.",
        [("backend/scratch_render.py",
          "    local = work / f\"{dest.stem}{suffix or dest.suffix}\"",
          "    local = dest")],
    ),
    (
        "motion.render_shot encodes straight into the render dir",
        "The observed ffmpeg invocation: `... /gcs/calluses/TestRun/render/s003/"
        "s003.06.mp4` as the output path, frames piped in on stdin.",
        [("backend/motion.py",
          "    return scratch_render.render_to(out_path, _encode)",
          "    _encode(out_path)\n    return out_path")],
    ),
    (
        "director.concat builds the beat clip on the bucket",
        "s001.mp4 and s006.mp4 on 2026-08-18. 82 ComposeObject 429s on one object.",
        [("backend/director.py",
          "    scratch_render.render_to(dest, _build)",
          "    _build(dest)")],
    ),
    (
        "a failed copy discards the finished render",
        "Removes the rescue, so a mount failure loses work a human paid for. The "
        "guardrail test ordered first in the file is the one that must catch it.",
        [("backend/scratch_render.py",
          "        kept = _rescue(src, dest)",
          "        kept = None")],
    ),
    (
        "publish copies straight onto the destination, never staging",
        "Weakens atomicity where rename IS available: a reader can now see a "
        "prefix of the new clip at a path that looks like a finished one.",
        [("backend/scratch_render.py",
          "        if _rename_supported(dest.parent):",
          "        if False:")],
    ),
    (
        "the staging file is named like a finished clip",
        "A `.mp4` in the render directory is servable by /media/ and matches the "
        "`*.mp4` checks that decide a clip exists. Half a clip would pass both.",
        [("backend/scratch_render.py",
          '_STAGE_SUFFIX = ".part"',
          '_STAGE_SUFFIX = ".mp4"')],
    ),
    (
        "the copy is not retried",
        "One throttled write now costs a beat instead of a second of backoff.",
        [("backend/scratch_render.py",
          "_COPY_ATTEMPTS = 5",
          "_COPY_ATTEMPTS = 1")],
    ),
    (
        "the rename probe runs on every publish",
        "Not a correctness defect -- a cost one. Two extra object mutations per "
        "clip is the shape of problem this whole change exists to remove.",
        [("backend/scratch_render.py",
          "    known = _RENAME_OK.get(key)",
          "    known = None")],
    ),
]


def _run_suite() -> int:
    proc = subprocess.run([sys.executable, "-m", "pytest", *SUITE, "-q"],
                          cwd=ROOT, capture_output=True, text=True)
    return proc.returncode


def main() -> int:
    print(f"interpreter: {sys.executable}")
    print(f"suite:       {' '.join(SUITE)}\n")

    baseline = _run_suite()
    if baseline != 0:
        print(f"BASELINE IS RED (exit {baseline}) — nothing below means anything.")
        return 1
    print("baseline: exit 0\n")

    survived: list[str] = []
    for name, why, edits in MUTATIONS:
        paths = {ROOT / rel for rel, _, _ in edits}
        before = {p: p.read_bytes() for p in paths}
        digests = {p: _digest(p) for p in paths}

        try:
            for rel, find, repl in edits:
                p = ROOT / rel
                text = p.read_text(encoding="utf-8")
                if text.count(find) != 1:
                    raise SystemExit(
                        f"{name}: anchor matched {text.count(find)} times in {rel} "
                        f"— the mutation is not faithful, fix it before trusting it")
                p.write_text(text.replace(find, repl, 1), encoding="utf-8")
            code = _run_suite()
        finally:
            for p, blob in before.items():
                p.write_bytes(blob)

        for p, want in digests.items():
            assert _digest(p) == want, f"{p} was not restored byte-for-byte"

        verdict = "KILLED  " if code != 0 else "SURVIVED"
        if code == 0:
            survived.append(name)
        print(f"  [{verdict}] {name}")
        print(f"             {why}")

    restored = _run_suite()
    print(f"\nrestored suite: exit {restored}")
    if survived:
        print(f"\n{len(survived)} MUTATION(S) SURVIVED — the tests do not cover them:")
        for s in survived:
            print(f"  - {s}")
        return 1
    print(f"\nall {len(MUTATIONS)} mutations killed; tree restored and hash-verified.")
    return 0 if restored == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
