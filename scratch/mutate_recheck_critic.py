"""Mutation-sensitivity check for the critic re-check outcome (contract 11.4).

The defect this guards is a silence, not a crash, so a test that merely proves
"nothing threw" is exactly what the broken code already passed. Each mutation
below restores one piece of that silence; the run is only meaningful if every
one is KILLED.

R1 is the original defect verbatim.

Run from the repo root:  python scratch/mutate_recheck_critic.py
"""
from __future__ import annotations

import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
WS = ROOT / "frontend/src/components/DirectorWorkspace.tsx"
API = ROOT / "frontend/src/lib/directorApi.ts"

TARGETS = [
    "src/lib/directorApi.critique.test.ts",
    "src/components/DirectorWorkspace.recheck.test.tsx",
]

CATCH_BLOCK = """    } catch (e) {
      setCritiqueOutcome({
        kind: "failed",
        message:
          `${(e as Error)?.message || `Re-checking ${beats.join(", ")} failed.`} ` +
          `Nothing on screen has been re-checked — any findings shown are the ` +
          `ones from before this run.`,
      });
    } finally {
      setCritiquing(false);"""

MUTATIONS = [
    # The defect as it actually shipped.
    ("R1  the original defect: log a failure as 'complete'", WS,
     CATCH_BLOCK,
     """    } catch (e) {
      console.log("Critique check complete");
    } finally {
      setCritiquing(false);"""),
    ("R2  the quiet path: believe any reply that did not throw", WS,
     "if (!res.ok || !Array.isArray(res.warnings)) {",
     "if (false) {"),
    ("R3  believe `warnings` even when `ok` is falsy", WS,
     "if (!res.ok || !Array.isArray(res.warnings)) {",
     "if (!Array.isArray(res.warnings)) {"),
    ("R4  a failure that does not say the screen is stale", WS,
     "`Nothing on screen has been re-checked — any findings shown are the ` +\n          `ones from before this run.`,",
     "``,"),
    ("R5  report every run as clean", WS,
     "fresh.length > 0\n          ? { kind: \"found\", count: fresh.length }\n          : { kind: \"clear\", count: 0 }",
     "{ kind: \"clear\", count: 0 }"),
    ("R6  put the outcome back inside the banner that unmounts", WS,
     "{(critiqueOutcome || unresolvedWarnings.length === 0) && (",
     "{(critiqueOutcome && unresolvedWarnings.length > 0) && ("),
    # Anchored on `setCritiquing(true)` too: handleCompileCoverage derives its
    # beats identically, and a two-hit anchor mutates nothing.
    ("R7  re-check only the open beat, not the scene", WS,
     "coveragePlan.scene_beats && coveragePlan.scene_beats.length > 0\n"
     "        ? coveragePlan.scene_beats\n        : [sceneId];\n"
     "    setCritiquing(true);",
     "[sceneId];\n    setCritiquing(true);"),
    ("R8  drop the re-check control once every finding is decided", WS,
     "{unresolvedWarnings.length === 0 && (\n            <button\n              data-testid=\"recheck-warnings-idle\"",
     "{false && (\n            <button\n              data-testid=\"recheck-warnings-idle\""),
    ("R9  api: drop `detail`, losing the 400 the route raises", API,
     "data.error ||\n        data.detail ||\n        `Re-checking ${beats.join(\", \")} failed with status ${res.status}`",
     "data.error ||\n        `Re-checking ${beats.join(\", \")} failed with status ${res.status}`"),
    ("R10 api: unguarded res.json(), so a gateway page becomes a SyntaxError", API,
     "const data: CritiqueReply = await res.json().catch(() => ({}));",
     "const data: CritiqueReply = await res.json();"),
]


def run_suite() -> tuple[int, str]:
    proc = subprocess.run(
        ["npx.cmd" if sys.platform == "win32" else "npx", "vitest", "run", *TARGETS],
        cwd=ROOT / "frontend", capture_output=True, text=True,
        encoding="utf-8", errors="replace",
    )
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def main() -> int:
    survivors = []
    for name, path, old, new in MUTATIONS:
        src = path.read_text(encoding="utf-8")
        if src.count(old) != 1:
            print(f"STALE    {name}: anchor matched {src.count(old)} times")
            survivors.append(name)
            continue
        try:
            path.write_text(src.replace(old, new), encoding="utf-8")
            code, out = run_suite()
        finally:
            path.write_text(src, encoding="utf-8")

        totals = [l.strip() for l in out.splitlines() if l.strip().startswith("Tests ")]
        failed = [l.strip() for l in out.splitlines() if l.strip().startswith(("x ", "×"))]
        print(f"{'KILLED  ' if code != 0 else 'SURVIVED'} {name}")
        for line in totals:
            print("         ", line)
        for line in failed[:8]:
            print("          -", line[:120])
        if code == 0:
            survivors.append(name)

    print()
    if survivors:
        print(f"{len(survivors)} mutation(s) SURVIVED -- the tests do not prove the fix:")
        for s in survivors:
            print("  -", s)
        return 1
    print(f"All {len(MUTATIONS)} mutations killed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
