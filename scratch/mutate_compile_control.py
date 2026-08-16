"""Mutation-sensitivity check for the studio's compile control.

A test that passes proves nothing on its own; what it must do is FAIL when the
fix it guards is taken away. Each entry below is a faithful mutation of one part
of the compile control -- the part it removes is exactly the defect it stands
for -- and the run is only meaningful if every one is KILLED.

DYING IS NOT ENOUGH; IT HAS TO DIE FOR THE RIGHT REASON.

That is the lesson this file was corrected for. M5 bypasses the cost gate -- the
money mutation, the one that matters most -- and it was being killed by a cheap
`getByTestId("compile-cost-gate")` throwing on a missing element, inside a
helper, BEFORE any test asserted that nothing had been dispatched. It therefore
proved the gate element renders, not that money cannot be spent without it. A
mutation killed by an unrelated assertion is a mutation that survived the
assertion it was written for.

So a mutation may declare:

  expect_fail    test names that MUST be among the failures
  expect_output  a substring that MUST appear in the run's output -- for M5 the
                 money assertion's own sentinel message, which can only be
                 printed if that assertion actually ran

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

# The money assertion's failure message, defined in the component test. Its
# presence in the output is proof that the assertion was reached and evaluated.
SPENT_WITHOUT_A_PRICE = (
    "COMPILE COVERAGE dispatched without the cost gate: "
    "money committed with no price shown"
)

# (name, file, old, new, expect_fail, expect_output)
MUTATIONS = [
    ("M1  flatten every refusal into one message", WS,
     'const message = err?.message || "The compile request failed before it started.";',
     'const message = "The compile failed.";',
     ["draft", "approval drift", "staleness", "unresolved warnings",
      "already running", "no plan at all", "the catch-all 400"], ""),
    ("M2  fire-and-forget: do not wait for the job", WS,
     "const done = await waitForJob(res.job, { onLog: setCompileLog });",
     'const done = { ok: true, status: "done", log: "" };',
     ["nothing claims compiled while the job is still running"], ""),
    ("M3  claim compiled from the job, not from the saved plan", WS,
     'if (fresh.status === "compiled") {',
     "if (true) {",
     ["a finished job is not the claim"], ""),
    ("M4  read approval_drifted as truthy, losing 'never locked'", WS,
     'if (err?.approvalDrifted === false) return { kind: "draft", message };',
     "",
     ["draft"], ""),
    # The money mutation. `expect_output` is the whole point of it: without the
    # sentinel this passed as "killed" while the dispatch assertion never ran.
    ("M5  spend without showing the price first", WS,
     "onClick={() => setCompileGateOpen((open) => !open)}",
     "onClick={handleCompileCoverage}",
     ["pressing COMPILE COVERAGE commits no money"], SPENT_WITHOUT_A_PRICE),
    ("M6  drop the 404's `detail`, keeping only `error`", API,
     "data.error || data.detail || `Compiling ${beatId} failed with status ${res.status}`",
     "data.error || `Compiling ${beatId} failed with status ${res.status}`",
     ["the 404 is raised as an HTTPException"], ""),
    ("M7  `if (data.approval_drifted)` instead of a type check", API,
     'if (typeof data.approval_drifted === "boolean") {',
     "if (data.approval_drifted) {",
     ["a draft plan says lock it first"], ""),
    # Anchored on the following line too: `err.status = res.status;` also
    # appears in redirectSceneCoverage, and a two-hit anchor mutates nothing.
    ("M8  swallow the status, so a refusal cannot be told from a failure", API,
     'err.status = res.status;\n    if (typeof data.approval_drifted === "boolean") {',
     'if (typeof data.approval_drifted === "boolean") {',
     ["a compile already running is a 409 with no discriminator at all"], ""),
    ("M9  compile only the first beat of a locked scene", WS,
     "for (const beat of beats) {",
     "for (const beat of beats.slice(0, 1)) {",
     ["every beat in the plan is compiled, in order"], ""),
    # --- the quote binding ---------------------------------------------------
    ("M10 send the beat id but not the plan it was quoted for", WS,
     "const res = await compileCoverage(beat, coveragePlan.approved_signature);",
     "const res = await compileCoverage(beat);",
     ["the plan's identity travels with the request"], ""),
    ("M11 drop the signature from the request URL", API,
     "const query = planSignature\n    ? `?plan_signature=${encodeURIComponent(planSignature)}`\n    : \"\";",
     'const query = "";',
     ["carries the signature of the plan the price was quoted for"], ""),
    ("M12 lose the signature_mismatch discriminator", API,
     "if (data.signature_mismatch) err.signatureMismatch = true;",
     "",
     ["a plan replaced after the quote comes back flagged as a mismatch"], ""),
    ("M13 classify a changed quote as an ordinary 409", WS,
     'if (err?.signatureMismatch) return { kind: "quote_changed", message };',
     "",
     ["the plan changed after the quote"], ""),
    ("M14 re-quote in the message but leave the stale plan on screen", WS,
     "const fresh = await fetchCoveragePlan(beats);\n          setCoveragePlan(fresh);",
     "const fresh = await fetchCoveragePlan(beats);",
     ["the refreshed plan is what the next attempt quotes"], ""),
    ("M15 do not re-quote at all", WS,
     "if (problem.kind === \"quote_changed\") {",
     "if (false) {",
     ["the plan changed after the quote",
      "the refreshed plan is what the next attempt quotes"], ""),
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
    for name, path, old, new, expect_fail, expect_output in MUTATIONS:
        src = path.read_text(encoding="utf-8")
        if src.count(old) != 1:
            print(f"STALE    {name}: anchor matched {src.count(old)} times")
            problems.append(f"{name}: stale anchor")
            continue
        try:
            path.write_text(src.replace(old, new), encoding="utf-8")
            code, out = run_suite()
        finally:
            path.write_text(src, encoding="utf-8")

        totals = [l.strip() for l in out.splitlines() if l.strip().startswith("Tests ")]
        failed = [l.strip() for l in out.splitlines() if l.strip().startswith(("x ", "×"))]
        verdict = "KILLED  " if code != 0 else "SURVIVED"

        # Killed, but by the right assertion?
        unmet = [w for w in expect_fail if not any(w in f for f in failed)]
        if code == 0:
            problems.append(f"{name}: survived")
        elif unmet:
            verdict = "MISFIRED"
            problems.append(f"{name}: killed, but not by {unmet}")
        elif expect_output and expect_output not in out:
            verdict = "MISFIRED"
            problems.append(
                f"{name}: killed without ever evaluating its own assertion "
                f"(sentinel absent from the output)")

        print(f"{verdict} {name}")
        for line in totals:
            print("         ", line)
        for line in failed[:8]:
            print("          -", line[:120])
        if expect_output:
            print(f"          reached its own assertion: "
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
