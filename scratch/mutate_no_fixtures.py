"""Mutation-sensitivity check: a fixture must not reach a human.

The defect: `DirectorWorkspace` handed the montage matrix the mock-scenes
fixture from `directorApi.ts` -- "s004 - The Mountain Takes Its Toll", 11 shots,
estimated_cost 3.82. The human opened the Director and was shown that invented
film, and that invented cost, while their real locked plan was not on screen.
The fixture's `shots_count` then sliced the REAL shot list, so genuine coverage
was truncated to the mock's shape.

F1 restores the defect exactly. The rest remove one guard each.

Three things this harness insists on, each of which it has already caught here:

  expect_fail    the test names that must be among the failures. A mutation
                 killed by something else survived the assertion it was written
                 for. (F2 was being killed by the wrong test, because the
                 workspace header quotes the same cost as the matrix row and the
                 assertion searched the whole document.)

  expect_output  a substring the run must contain. F1's is the fixture's own
                 title, so a run only counts if the fabricated film actually
                 appeared on screen. (This caught F1 itself being UNFAITHFUL:
                 the fix removed the fixture's import as well as its use, so a
                 one-line mutation left an undefined identifier and the tests
                 died on a ReferenceError instead of on a fixture being shown.
                 A mutation that crashes proves nothing -- F1 is now two edits.)

  a target list  that reaches the code being mutated. F5 was reported SURVIVED
                 until `directorApi.scene.test.ts` was added, because the `??`
                 fix lives in `fetchCoveragePlan`, which every component suite
                 mocks -- so no component test could ever have exercised it.

Run from the repo root:  python scratch/mutate_no_fixtures.py
"""
from __future__ import annotations

import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
WS = ROOT / "frontend/src/components/DirectorWorkspace.tsx"
API = ROOT / "frontend/src/lib/directorApi.ts"
MATRIX = ROOT / "frontend/src/components/CompactMontageMatrix.tsx"
# -21's scanner, on main since ceaaab6. This branch had a near-duplicate;
# keeping two tree scanners that disagree is worse than one that is right, and
# theirs is right on the point they differed: it strips comments, so the record
# of what went wrong can stay in the code beside the fix.
GUARD = ROOT / "frontend/src/lib/noMockData.test.ts"

TARGETS = [
    "src/components/DirectorWorkspace.nomock.test.tsx",
    "src/lib/noMockData.test.ts",
    "src/lib/directorApi.scene.test.ts",
]

# The fixture's own title. If it appears in a failure, a test genuinely caught
# the fabricated film on screen rather than tripping over a crash.
FIXTURE_TITLE = "The Mountain Takes Its Toll"

# (name, [(file, old, new), ...], expect_fail, expect_output)
MUTATIONS = [
    ("F1  the original defect: hand the matrix the fixture",
     [(WS, "scenes={[sceneSummaryOf(coveragePlan, sceneId)]}", "scenes={MOCK_SCENES}"),
      # Both halves, or this is a ReferenceError and not the defect.
      (WS, 'import CoverageRhythmBeatSheet from "./CoverageRhythmBeatSheet";',
       'import CoverageRhythmBeatSheet from "./CoverageRhythmBeatSheet";\n'
       'import { MOCK_SCENES } from "../lib/directorApi";')],
     ["no part of MOCK_SCENES reaches the screen",
      "the row is the loaded plan",
      "no production file imports a fixture except the known, tracked ones",
      "the montage matrix is not one of them"],
     FIXTURE_TITLE),
    ("F2  quote a cost that is not the server's summary",
     [(WS, "estimated_cost: plan.estimated_cost,", "estimated_cost: 3.82,")],
     ["the row is the loaded plan"], ""),
    ("F3  count the shots from anywhere but the coverage",
     [(WS, "shots_count: plan.coverage.length,", "shots_count: 11,")],
     ["real coverage is not truncated to a fixture's shape"], ""),
    ("F4  go back to a hardcoded shot count in the header",
     [(MATRIX, "{allShots.length}-Shot Bird&apos;s-Eye View",
       "158-Shot Bird&apos;s-Eye View")],
     ["the shot count is counted, not asserted",
      "real coverage is not truncated to a fixture's shape"], ""),
    ("F5  `||` again, so a free scene falls through to a stale price",
     [(API, "estimated_cost: data.summary?.estimated_cost ?? plan.estimated_cost ?? 0,",
       "estimated_cost: data.summary?.estimated_cost || plan.estimated_cost || 0,")],
     ["a free scene is quoted at zero"], ""),
    ("F5b `||` on the paid count, so zero paid shots reads as unknown",
     [(API, "data.summary?.paid_shots ??", "data.summary?.paid_shots ||")],
     ["zero paid shots is a count, not a missing one"], ""),
    # --- the guard's own guards ---------------------------------------------
    # A scan that scans nothing reports no breaches, and an allowlist entry that
    # outlives its defect silently widens the rule for ever. Both are how this
    # kind of test rots into decoration.
    ("F6  break the source walk, so the scan finds nothing to object to",
     [(GUARD, "if (!/\\.tsx?$/.test(entry.name)) continue;", "if (true) continue;")],
     ["the scan is real"], ""),
    ("F7  re-exempt a file that is no longer in breach, and never notice",
     [(GUARD, "const KNOWN_OFFENDERS: Record<string, string> = {};",
       "const KNOWN_OFFENDERS: Record<string, string> = {\n"
       '  "components/DirectorWorkspace.tsx": "stale exemption nobody removed",\n'
       "};")],
     ["every known offender is real"], ""),
]


def run_suite() -> tuple[int, str]:
    proc = subprocess.run(
        ["npx.cmd" if sys.platform == "win32" else "npx", "vitest", "run", *TARGETS],
        cwd=ROOT / "frontend", capture_output=True, text=True,
        encoding="utf-8", errors="replace",
    )
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def main() -> int:
    problems: list[str] = []
    for name, edits, expect_fail, expect_output in MUTATIONS:
        originals = {path: path.read_text(encoding="utf-8") for path, _, _ in edits}

        stale = [old for path, old, _ in edits if originals[path].count(old) != 1]
        if stale:
            print(f"STALE    {name}: {len(stale)} anchor(s) did not match exactly once")
            problems.append(f"{name}: stale anchor")
            continue

        try:
            pending = dict(originals)
            for path, old, new in edits:
                pending[path] = pending[path].replace(old, new)
            for path, text in pending.items():
                path.write_text(text, encoding="utf-8")
            code, out = run_suite()
        finally:
            for path, text in originals.items():
                path.write_text(text, encoding="utf-8")

        totals = [l.strip() for l in out.splitlines() if l.strip().startswith("Tests ")]
        failed = [l.strip() for l in out.splitlines() if l.strip().startswith(("x ", "×"))]
        verdict = "KILLED  " if code != 0 else "SURVIVED"

        unmet = [w for w in expect_fail if not any(w in f for f in failed)]
        if code == 0:
            problems.append(f"{name}: survived")
        elif unmet:
            verdict = "MISFIRED"
            problems.append(f"{name}: killed, but not by {unmet}")
        elif expect_output and expect_output not in out:
            verdict = "MISFIRED"
            problems.append(
                f"{name}: killed without the fixture appearing in the failure, "
                f"so nothing proved it reached the screen")

        print(f"{verdict} {name}")
        for line in totals:
            print("         ", line)
        for line in failed[:8]:
            print("          -", line[:120])
        if expect_output:
            print(f"          the fixture is visible in the failure: "
                  f"{'yes' if expect_output in out else 'NO'}")

    print()
    if problems:
        print(f"{len(problems)} mutation(s) did not prove what they stand for:")
        for p in problems:
            print("  -", p)
        return 1
    print(f"All {len(MUTATIONS)} mutations killed, each by the assertion it stands for.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
