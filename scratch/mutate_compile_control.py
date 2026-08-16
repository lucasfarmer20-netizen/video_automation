"""Mutation-sensitivity check for the studio's compile control.

A test that passes proves nothing on its own; what it must do is FAIL when the
fix it guards is taken away. Each entry below is a faithful mutation of one part
of the compile control -- the part it removes is exactly the defect the brief
named -- and the run is only meaningful if every one is KILLED.

Run from the repo root:  python scratch/mutate_compile_control.py
"""
from __future__ import annotations

import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
WS = ROOT / "frontend/src/components/DirectorWorkspace.tsx"
API = ROOT / "frontend/src/lib/directorApi.ts"

TARGETS = [
    "src/lib/directorApi.compile.test.ts",
    "src/components/DirectorWorkspace.compile.test.tsx",
]

MUTATIONS = [
    ("M1  flatten every refusal into one message", WS,
     'const message = err?.message || "The compile request failed before it started.";',
     'const message = "The compile failed.";'),
    ("M2  fire-and-forget: do not wait for the job", WS,
     "const done = await waitForJob(res.job, { onLog: setCompileLog });",
     'const done = { ok: true, status: "done", log: "" };'),
    ("M3  claim compiled from the job, not from the saved plan", WS,
     'if (fresh.status === "compiled") {',
     "if (true) {"),
    ("M4  read approval_drifted as truthy, losing 'never locked'", WS,
     'if (err?.approvalDrifted === false) return { kind: "draft", message };',
     ""),
    ("M5  spend without showing the price first", WS,
     "onClick={() => setCompileGateOpen((open) => !open)}",
     "onClick={handleCompileCoverage}"),
    ("M6  drop the 404's `detail`, keeping only `error`", API,
     "data.error || data.detail || `Compiling ${beatId} failed with status ${res.status}`",
     "data.error || `Compiling ${beatId} failed with status ${res.status}`"),
    ("M7  `if (data.approval_drifted)` instead of a type check", API,
     'if (typeof data.approval_drifted === "boolean") {',
     "if (data.approval_drifted) {"),
    # Anchored on the following line too: `err.status = res.status;` also
    # appears in redirectSceneCoverage, and a two-hit anchor mutates nothing.
    ("M8  swallow the status, so a refusal cannot be told from a failure", API,
     'err.status = res.status;\n    if (typeof data.approval_drifted === "boolean") {',
     'if (typeof data.approval_drifted === "boolean") {'),
    ("M9  compile only the first beat of a locked scene", WS,
     "for (const beat of beats) {",
     "for (const beat of beats.slice(0, 1)) {"),
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
