"""Mutation-sensitivity check for the compile route's quote binding.

The defect: `compile_director_coverage` accepted no plan identity, so it
dispatched `director.load_plan(beat_id)` -- whatever was current at DISPATCH --
against a price the human had confirmed for some earlier plan. Replace and lock
a more expensive plan in another tab and it compiled at the new price on the old
consent.

Every mutation below removes one part of the binding. The run is only meaningful
if each is KILLED, and killed BY THE ASSERTION IT STANDS FOR -- so each declares
the test names that must be among the failures. A mutation killed by an
unrelated assertion is a mutation that survived the one it was written for; that
is not hypothetical, it is the defect this project has now hit five times.

Run from the repo root:  python scratch/mutate_quote_binding.py
"""
from __future__ import annotations

import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
MAIN = ROOT / "backend/main.py"

TARGET = "tests/test_compile_quote_binding.py"

# The refusal block, as it stands in the route.
BINDING = '''        current_signature = director.plan_signature(plan)
        if plan_signature and plan_signature != current_signature:
            return JSONResponse(status_code=409, content={
                "ok": False,
                "error": (f"the plan for {beat_id} changed after you were quoted a "
                          f"price, so nothing was compiled and nothing was charged. "
                          f"Review the current plan and confirm its cost again."),
                "signature_mismatch": True,
                "quoted_signature": plan_signature,
                "plan_signature": current_signature,
            })'''

# The dispatch assertion's own failure message. Requiring it in the output is
# what proves Q1 died on "money was spent" and not on "expected 409, got 200" --
# the distinction this project keeps having to relearn.
DISPATCHED = "dispatched with shot counts"

# (name, old, new, expect_fail, expect_output)
MUTATIONS = [
    # The defect itself. `current_signature` stays, because the draft branch
    # reads it -- removing that too would be a NameError, which is a crash and
    # not the defect.
    ("Q1  the original defect: no binding at all",
     BINDING,
     "        current_signature = director.plan_signature(plan)",
     ["test_a_plan_replaced_after_the_quote_is_not_compiled",
      "test_the_refusal_hands_back_the_signature_needed_to_re_quote",
      "test_an_edit_that_does_not_change_cost_still_invalidates_the_quote",
      "test_consent_is_checked_before_the_plan_s_own_state"],
     DISPATCHED),
    ("Q2  refuse every compile that carries a signature",
     "if plan_signature and plan_signature != current_signature:",
     "if plan_signature:",
     ["test_the_quoted_plan_compiles"], ""),
    ("Q3  treat a caller that quoted nothing as a mismatch",
     "if plan_signature and plan_signature != current_signature:",
     "if plan_signature != current_signature:",
     ["test_a_caller_that_quoted_no_price_is_unaffected",
      "test_an_empty_signature_is_not_treated_as_a_mismatch"], ""),
    ("Q4  compare the shape of the signatures, not the signatures",
     "if plan_signature and plan_signature != current_signature:",
     "if plan_signature and len(plan_signature) != len(current_signature):",
     ["test_a_plan_replaced_after_the_quote_is_not_compiled",
      "test_an_edit_that_does_not_change_cost_still_invalidates_the_quote"], ""),
    ("Q5  refuse, but with a 200 the caller will read as success",
     '            return JSONResponse(status_code=409, content={\n'
     '                "ok": False,\n'
     '                "error": (f"the plan for {beat_id} changed after you were quoted a "',
     '            return JSONResponse(status_code=200, content={\n'
     '                "ok": False,\n'
     '                "error": (f"the plan for {beat_id} changed after you were quoted a "',
     ["test_a_plan_replaced_after_the_quote_is_not_compiled"], ""),
    ("Q6  drop the discriminator the client branches on",
     '                "signature_mismatch": True,\n                "quoted_signature": plan_signature,',
     '                "quoted_signature": plan_signature,',
     ["test_a_plan_replaced_after_the_quote_is_not_compiled",
      "test_consent_is_checked_before_the_plan_s_own_state"], ""),
    ("Q7  refuse without saying what the plan is now, so no re-quote is possible",
     '                "plan_signature": current_signature,\n            })\n'
     '        # §11.5: an approved plan must not silently mutate after approval.',
     '                "plan_signature": "",\n            })\n'
     '        # §11.5: an approved plan must not silently mutate after approval.',
     ["test_the_refusal_hands_back_the_signature_needed_to_re_quote"], ""),
]


def run_suite() -> tuple[int, str]:
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", TARGET, "-q"],
        cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def main() -> int:
    problems: list[str] = []
    for name, old, new, expect_fail, expect_output in MUTATIONS:
        src = MAIN.read_text(encoding="utf-8")
        if src.count(old) != 1:
            print(f"STALE    {name}: anchor matched {src.count(old)} times")
            problems.append(f"{name}: stale anchor")
            continue
        try:
            MAIN.write_text(src.replace(old, new), encoding="utf-8")
            code, out = run_suite()
        finally:
            MAIN.write_text(src, encoding="utf-8")

        failed = [l.strip() for l in out.splitlines() if l.strip().startswith("FAILED")]
        totals = [l.strip() for l in out.splitlines()
                  if " passed" in l or " failed" in l and "=" not in l]
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
                f"{name}: killed without ever evaluating the dispatch assertion "
                f"(its message is absent from the output)")

        print(f"{verdict} {name}")
        for line in totals[-1:]:
            print("         ", line)
        for line in failed[:8]:
            print("          -", line[:120])
        if expect_output:
            print(f"          died on the dispatch assertion: "
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
